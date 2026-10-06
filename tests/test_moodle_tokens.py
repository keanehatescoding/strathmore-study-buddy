"""Per-user Moodle tokens: crypto, token.php exchange, resolution, settings UI."""

import io
import json
import re
import urllib.parse

import pytest
from sqlmodel import select

from app import moodle_tokens
from app.config import settings
from app.models import Course, Job, Resource, Topic, User
from app.moodle import MoodleError
from app.moodle_tokens import decrypt_token, encrypt_token, fetch_token, token_for


@pytest.fixture()
def shared_token(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token", "GLOBAL")
    monkeypatch.setattr(settings, "moodle_token_owner", "owner@x.edu")


def test_encrypt_roundtrip_and_not_plaintext():
    stored = encrypt_token("abc123")
    assert "abc123" not in stored
    assert decrypt_token(stored) == "abc123"


def test_decrypt_fails_closed(monkeypatch):
    stored = encrypt_token("abc123")
    monkeypatch.setattr(settings, "secret_key", "rotated")
    assert decrypt_token(stored) is None
    assert decrypt_token("plaintext-legacy") is None
    assert decrypt_token(None) is None


def test_fetch_token_posts_credentials_in_body(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = urllib.parse.parse_qs(req.data.decode())
        return io.BytesIO(json.dumps({"token": "T0K"}).encode())

    monkeypatch.setattr(moodle_tokens.urllib.request, "urlopen", fake_urlopen)
    assert fetch_token("https://m.example/", "stud", "s3cret") == "T0K"
    assert seen["url"] == "https://m.example/login/token.php"
    assert "s3cret" not in seen["url"]
    assert seen["body"]["service"] == ["moodle_mobile_app"]


def test_fetch_token_error_hides_password(monkeypatch):
    monkeypatch.setattr(
        moodle_tokens.urllib.request, "urlopen",
        lambda req, timeout: io.BytesIO(b'{"error": "Invalid login, please try again"}'),
    )
    with pytest.raises(MoodleError) as exc:
        fetch_token("https://m.example", "stud", "s3cret")
    assert "Invalid login" in str(exc.value)
    assert "s3cret" not in str(exc.value)


def test_token_for_prefers_own_token(shared_token):
    assert token_for(User(email="a@x.edu", moodle_token=encrypt_token("MINE"))) == "MINE"


def test_shared_token_only_for_owner(shared_token):
    assert token_for(User(email="Owner@x.edu")) == "GLOBAL"
    assert token_for(User(email="someone@x.edu")) is None


def test_shared_token_for_nobody_when_owner_unset(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token", "GLOBAL")
    monkeypatch.setattr(settings, "moodle_token_owner", "")
    assert token_for(User(email="someone@x.edu")) is None
    assert token_for(None) is None


def test_build_adapter_uses_users_token(shared_token):
    from app.sync_cli import NotConnectedError, build_adapter

    adapter = build_adapter("moodle", User(email="a@x.edu", moodle_token=encrypt_token("MINE")))
    assert adapter.client.token == "MINE"
    with pytest.raises(NotConnectedError):
        build_adapter("moodle", User(email="b@x.edu"))


def test_all_users_selects_only_connected(shared_token):
    from app.crypto import seal
    from app.sync_cli import has_credentials

    assert has_credentials("moodle", User(email="a@x.edu", moodle_token=encrypt_token("A")))
    assert has_credentials("moodle", User(email="owner@x.edu"))
    assert not has_credentials("moodle", User(email="b@x.edu"))
    sealed = seal("google-refresh-token", "r")
    assert has_credentials("classroom", User(email="b@x.edu", google_refresh_token=sealed))
    assert not has_credentials("classroom", User(email="b@x.edu", google_refresh_token="r"))


def test_pipeline_downloads_as_course_owner(testapp, shared_token):
    from app.pipeline import moodle_downloader_for, run_extraction

    with testapp["Session"]() as s:
        alice = User(email="alice@x.edu", moodle_token=encrypt_token("ALICE"))
        bob = User(email="bob@x.edu")  # not connected
        s.add_all([alice, bob])
        s.commit()
        resources = {}
        for owner in (alice, bob):
            course = Course(source="moodle", source_id=owner.email, name="C", user_id=owner.id)
            s.add(course)
            s.commit()
            topic = Topic(course_id=course.id, source_id="t", title="T")
            s.add(topic)
            s.commit()
            r = Resource(topic_id=topic.id, source="moodle", source_id="r", type="file",
                         title="notes", raw_url="https://m.example/f.txt", status="pending")
            s.add(r)
            s.commit()
            resources[owner.email] = r.id

        # an unowned pre-auth course never gets the shared token
        orphan = Course(source="moodle", source_id="orphan", name="C")
        s.add(orphan)
        s.commit()
        orphan_topic = Topic(course_id=orphan.id, source_id="t", title="T")
        s.add(orphan_topic)
        s.commit()
        orphan_r = Resource(topic_id=orphan_topic.id, source="moodle", source_id="r",
                            type="file", title="notes", status="pending",
                            raw_url="https://m.example/f.txt")
        s.add(orphan_r)
        s.commit()

        downloader_for = moodle_downloader_for(s)
        assert downloader_for(orphan_r) is None
        alice_dl = downloader_for(s.get(Resource, resources["alice@x.edu"]))
        assert alice_dl.__self__.token == "ALICE"
        assert downloader_for(s.get(Resource, resources["bob@x.edu"])) is None

        counts = run_extraction(s, None, downloader_for=lambda r: None).counts
        assert counts["no_token"] == 3  # alice, bob, orphan
        assert all(s.get(Resource, rid).status == "pending" for rid in resources.values())


def _csrf(client) -> str:
    page = client.get("/settings/moodle")
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_settings_page_renders(testapp):
    page = testapp["client"].get("/settings/moodle")
    assert page.status_code == 200
    assert "Not connected" in page.text


def test_connect_with_password_stores_encrypted_and_enqueues(testapp, monkeypatch):
    client = testapp["client"]
    monkeypatch.setattr(moodle_tokens, "fetch_token", lambda base, u, p: "NEWTOKEN")
    monkeypatch.setattr(moodle_tokens, "verify_token", lambda base, t: {"fullname": "Test S"})
    token = _csrf(client)
    resp = client.post("/settings/moodle/login", data={
        "csrf_token": token, "username": "stud", "password": "s3cret",
    })
    assert resp.status_code == 200  # followed the redirect
    assert "Connected as Test S" in resp.text
    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        assert user.moodle_token and "NEWTOKEN" not in user.moodle_token
        assert decrypt_token(user.moodle_token) == "NEWTOKEN"
        assert "s3cret" not in user.moodle_token
        jobs = s.exec(select(Job)).all()
        assert [j.payload["user_email"] for j in jobs] == ["test@x.edu"]


def test_reconnecting_moodle_does_not_stack_syncs(testapp, monkeypatch):
    # a double-submitted form, or reconnecting before the first sync ran
    client = testapp["client"]
    monkeypatch.setattr(moodle_tokens, "verify_token", lambda base, t: {"fullname": "Test S"})
    for _ in range(2):
        client.post("/settings/moodle/token", data={
            "csrf_token": _csrf(client), "token": "TOKEN"})
    with testapp["Session"]() as s:
        jobs = s.exec(select(Job)).all()
        assert [(j.type, j.payload["source"]) for j in jobs] == [("sync", "moodle")]


def test_moodle_password_attempts_are_limited(testapp, monkeypatch):
    # the form checks passwords against Moodle: without a tight limit it is
    # a password-guessing oracle for other students' accounts
    from app.main import app

    client = testapp["client"]
    tried = []

    def wrong_password(base, username, password):
        tried.append((username, password))
        raise MoodleError("Invalid login, please try again")

    monkeypatch.setattr(moodle_tokens, "fetch_token", wrong_password)
    app.state.moodle_login_limit = (3, 900)
    try:
        for guess in ("a", "b", "c", "d"):
            resp = client.post("/settings/moodle/login", data={
                "csrf_token": _csrf(client), "username": "victim", "password": guess,
            })
        assert "Too many Moodle sign-in attempts" in resp.text
        assert [p for _, p in tried] == ["a", "b", "c"]
        # also per account: other usernames don't reset the budget
        client.post("/settings/moodle/login", data={
            "csrf_token": _csrf(client), "username": "someone-else", "password": "e",
        })
        assert len(tried) == 3
    finally:
        del app.state.moodle_login_limit


def test_moodle_username_limit_spans_accounts(testapp, monkeypatch):
    from starlette.requests import Request

    from app.main import _moodle_login_wait, app

    app.state.moodle_login_limit = (2, 900)
    try:
        request = Request({"type": "http", "app": app})
        for n in range(2):
            assert _moodle_login_wait(request, User(email=f"u{n}@x.edu"), "Victim") is None
        assert _moodle_login_wait(request, User(email="u9@x.edu"), "victim") is not None
    finally:
        del app.state.moodle_login_limit


def test_rejected_token_is_not_saved(testapp, monkeypatch):
    client = testapp["client"]

    def reject(base, t):
        raise MoodleError("Invalid token - token not found")

    monkeypatch.setattr(moodle_tokens, "verify_token", reject)
    resp = client.post("/settings/moodle/token", data={
        "csrf_token": _csrf(client), "token": "bogus",
    })
    assert "Invalid token" in resp.text
    with testapp["Session"]() as s:
        assert s.get(User, testapp["user_id"]).moodle_token is None


def test_disconnect_clears_token(testapp):
    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        user.moodle_token = encrypt_token("X")
        s.add(user)
        s.commit()
    client = testapp["client"]
    resp = client.post("/settings/moodle/disconnect", data={"csrf_token": _csrf(client)})
    assert "Disconnected" in resp.text
    with testapp["Session"]() as s:
        assert s.get(User, testapp["user_id"]).moodle_token is None


@pytest.mark.parametrize("path", [
    "/settings/moodle/login", "/settings/moodle/token", "/settings/moodle/disconnect",
])
def test_settings_posts_require_csrf(testapp, path):
    resp = testapp["client"].post(path, data={"token": "x", "username": "u", "password": "p"})
    assert resp.status_code == 403


def test_download_only_to_own_pluginfile(monkeypatch):
    from app.moodle import ForeignURLError, MoodleClient

    client = MoodleClient("https://m.example/moodle", "TOKEN")
    assert client.serves("https://m.example/moodle/webservice/pluginfile.php/1/a.pdf")
    assert client.serves("https://M.example/moodle/pluginfile.php/1/a.pdf")
    for url in ("https://drive.google.com/file/d/x/view",
                "https://m.example.evil.com/moodle/pluginfile.php/1/a.pdf",
                "http://m.example/moodle/pluginfile.php/1/a.pdf",
                "https://m.example/other/pluginfile.php/1/a.pdf",
                "https://m.example/moodle/login/index.php"):
        assert not client.serves(url), url

    def no_network(*a, **kw):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(moodle_tokens.urllib.request, "urlopen", no_network)
    with pytest.raises(ForeignURLError) as exc:
        client.download("https://drive.google.com/file/d/x/view")
    assert "TOKEN" not in str(exc.value)


def test_classroom_shared_token_only_for_owner(monkeypatch):
    from app.auth import classroom_token_for
    from app.crypto import seal
    from app.sync_cli import has_credentials

    monkeypatch.setattr(settings, "google_refresh_token", "SHARED")
    monkeypatch.setattr(settings, "google_refresh_token_owner", "")
    assert classroom_token_for(User(email="a@x.edu")) is None
    monkeypatch.setattr(settings, "google_refresh_token_owner", "Owner@x.edu")
    assert classroom_token_for(User(email="owner@x.edu")) == "SHARED"
    assert classroom_token_for(User(email="a@x.edu")) is None
    assert not has_credentials("classroom", User(email="a@x.edu"))
    assert has_credentials("classroom", User(email="owner@x.edu"))
    own = User(email="a@x.edu", google_refresh_token=seal("google-refresh-token", "MINE"))
    assert classroom_token_for(own) == "MINE"


def test_unowned_shared_token_warns():
    from app.config import Settings

    with pytest.warns(UserWarning, match="MOODLE_TOKEN_OWNER"):
        Settings(_env_file=None, moodle_token="T", moodle_token_owner="")


def test_settings_page_explains_a_rejected_key(testapp):
    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        user.moodle_token = moodle_tokens.REJECTED
        s.add(user)
        s.commit()
    page = testapp["client"].get("/settings/moodle")
    assert "Not connected" in page.text
    assert "Moodle stopped accepting your saved key" in page.text
