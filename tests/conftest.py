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
from app.security import hit_table
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
    hit_table(app, "moodle_login_hits").clear()  # app is shared across tests
    yield {"client": TestClient(app), "Session": lambda: Session(engine),
           "user_id": user_id}
    app.dependency_overrides.clear()
