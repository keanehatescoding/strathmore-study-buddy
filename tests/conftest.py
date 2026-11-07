"""Shared TestClient with isolated in-memory SQLite per test.

Authenticated: current_user is overridden to a fixture user, so web tests
exercise the owned-data paths. Auth flow itself is tested in test_auth.py.
"""

import os

# settings are read at import: the public default SECRET_KEY needs DEV=1
os.environ.setdefault("DEV", "1")

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

from app.db import get_session
from app.main import app, current_user
from app.models import User
from tests.dbutil import TEST_DATABASE_URL, make_engine, reset_postgres


@pytest.fixture(autouse=True)
def _fresh_postgres():
    if TEST_DATABASE_URL:
        reset_postgres()


@pytest.fixture()
def testapp():
    engine = make_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="test@x.edu")
        s.add(user)
        s.commit()
        s.refresh(user)
        user_id = user.id

    def override_session():
        with Session(engine) as s:
            yield s

    def override_user(request: Request):
        with Session(engine) as s:
            user = s.get(User, user_id)
        request.state.user = user  # as current_user does, for nav_context
        return user

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[current_user] = override_user
    yield {"client": TestClient(app), "Session": lambda: Session(engine),
           "user_id": user_id}
    app.dependency_overrides.clear()


@pytest.fixture()
def browser_page(testapp):
    """A headless Chromium page whose requests to http://testserver are served
    by testapp's TestClient, so pages run their real app.js against test data.

    Skips when Playwright's Chromium isn't installed (`uv run playwright install
    chromium`), except on CI, where a missing browser fails the test instead.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    client = testapp["client"]

    def serve(route):
        req = route.request
        r = client.request(req.method, req.url, headers=req.headers,
                           content=req.post_data_buffer, follow_redirects=False)
        route.fulfill(status=r.status_code, headers=dict(r.headers), body=r.content)

    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except sync_api.Error as e:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"Chromium for Playwright not installed: {e.message.splitlines()[0]}")
        page = browser.new_page()
        page.route("http://testserver/**", serve)
        yield page
        browser.close()
