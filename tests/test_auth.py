"""Auth tests: login gating, cross-user isolation, sign-in claiming."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, select

import app.auth as auth_mod
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import Course, User
from tests.dbutil import make_engine


def test_unauthenticated_redirects_to_login():
    engine = make_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override_session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app, follow_redirects=False)
        r = client.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = client.get("/review")
        assert r.status_code == 303
        assert client.get("/health").status_code == 200
        assert "Sign in with Google" in client.get("/login").text
    finally:
        app.dependency_overrides.clear()


def test_login_url_requests_offline_classroom_scopes():
    url = auth_mod.login_url("cid", "https://x/cb", "state123")
    assert "access_type=offline" in url
    assert "state=state123" in url
    assert "classroom" in url and "openid" in url


def _memory_session():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_sign_in_upserts_and_claims(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token_owner", "new@x.edu")
    with _memory_session() as s:
        s.add(Course(source="moodle", source_id="c9", name="Orphan"))
        s.commit()
        user = auth_mod.sign_in(s, "new@x.edu", refresh_token="rt-1")
        assert auth_mod.refresh_token_for(user) == "rt-1"
        orphan = s.exec(select(Course)).one()
        assert orphan.user_id == user.id
        # second sign-in refreshes token, keeps id
        again = auth_mod.sign_in(s, "new@x.edu", refresh_token="rt-2")
        assert again.id == user.id and auth_mod.refresh_token_for(again) == "rt-2"


def test_refresh_token_encrypted_at_rest():
    with _memory_session() as s:
        user = auth_mod.sign_in(s, "a@x.edu", refresh_token="rt-secret")
        assert user.google_refresh_token.startswith("v1:")
        assert "rt-secret" not in user.google_refresh_token
        # plaintext left over from before encryption is not trusted
        assert auth_mod.refresh_token_for(User(email="b@x.edu", google_refresh_token="rt")) is None


def test_first_account_does_not_claim_without_owner(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token_owner", "")
    with _memory_session() as s:
        s.add(Course(source="moodle", source_id="c9", name="Orphan"))
        s.commit()
        auth_mod.sign_in(s, "first@x.edu")
        assert s.exec(select(Course)).one().user_id is None


def test_second_user_does_not_steal_unowned_courses(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token_owner", "")
    with _memory_session() as s:
        first = auth_mod.sign_in(s, "a@x.edu")
        s.add(Course(source="moodle", source_id="late", name="Synced later"))
        s.commit()
        auth_mod.sign_in(s, "b@x.edu")  # new, but not the first account
        auth_mod.sign_in(s, "a@x.edu")  # returning, not new
        assert s.exec(select(Course)).one().user_id is None
        assert first.id is not None


def test_only_token_owner_claims_unowned_courses(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token_owner", "Owner@x.edu")
    with _memory_session() as s:
        s.add(Course(source="moodle", source_id="c9", name="Orphan"))
        s.commit()
        auth_mod.sign_in(s, "someone@x.edu")  # first account, but not the owner
        assert s.exec(select(Course)).one().user_id is None
        owner = auth_mod.sign_in(s, "owner@x.edu")
        assert s.exec(select(Course)).one().user_id == owner.id


def test_token_owner_sign_in_claims_only_moodle_courses(monkeypatch):
    # the owner's claim comes from the shared Moodle token, so an unowned
    # Classroom course stays unowned; the CLI can still assign it
    monkeypatch.setattr(settings, "moodle_token_owner", "owner@x.edu")
    with _memory_session() as s:
        s.add(Course(source="moodle", source_id="m1", name="Moodle orphan"))
        s.add(Course(source="classroom", source_id="g1", name="Classroom orphan"))
        s.commit()
        owner = auth_mod.sign_in(s, "owner@x.edu")
        owners = {c.source: c.user_id for c in s.exec(select(Course)).all()}
        assert owners == {"moodle": owner.id, "classroom": None}
        assert auth_mod.claim_unowned(s, owner) == (1, 0)


def test_claim_unowned_assigns_only_orphans():
    with _memory_session() as s:
        other = auth_mod.sign_in(s, "other@x.edu")
        s.add(Course(source="moodle", source_id="o1", name="Orphan 1"))
        s.add(Course(source="moodle", source_id="o2", name="Orphan 2"))
        s.add(Course(source="moodle", source_id="m", name="Mine", user_id=other.id))
        s.commit()
        target = auth_mod.sign_in(s, "target@x.edu")
        assert auth_mod.claim_unowned(s, target) == (2, 0)
        owners = {c.source_id: c.user_id for c in s.exec(select(Course)).all()}
        assert owners == {"o1": target.id, "o2": target.id, "m": other.id}
        assert auth_mod.claim_unowned(s, target) == (0, 0)


def test_claim_unowned_skips_courses_user_already_has():
    # (user_id, source, source_id) is unique: claiming a duplicate would fail
    # the whole commit, so it is skipped and the rest are still claimed
    with _memory_session() as s:
        target = auth_mod.sign_in(s, "target@x.edu")
        s.add(Course(source="moodle", source_id="dup", name="Mine", user_id=target.id))
        s.add(Course(source="moodle", source_id="dup", name="Old copy"))
        s.add(Course(source="moodle", source_id="new", name="Orphan"))
        s.commit()
        assert auth_mod.claim_unowned(s, target) == (1, 1)
        rows = {(c.source_id, c.name): c.user_id for c in s.exec(select(Course)).all()}
        assert rows == {("dup", "Mine"): target.id, ("dup", "Old copy"): None,
                        ("new", "Orphan"): target.id}


def test_email_is_case_insensitive():
    with _memory_session() as s:
        a = auth_mod.sign_in(s, " Test@X.edu ")
        b = auth_mod.sign_in(s, "test@x.edu")
        assert a.id == b.id and b.email == "test@x.edu"
        assert len(s.exec(select(User)).all()) == 1


def test_legacy_mixed_case_row_is_matched_and_normalized():
    with _memory_session() as s:
        legacy = User(email="Legacy@X.edu")
        s.add(legacy)
        s.commit()
        user = auth_mod.sign_in(s, "legacy@x.edu")
        assert user.id == legacy.id and user.email == "legacy@x.edu"


def _userinfo(monkeypatch, info: dict):
    import io
    import json

    monkeypatch.setattr(
        auth_mod.urllib.request, "urlopen",
        lambda req, timeout: io.BytesIO(json.dumps(info).encode()),
    )


def test_fetch_email_requires_verified(monkeypatch):
    _userinfo(monkeypatch, {"email": "a@x.edu", "email_verified": False})
    with pytest.raises(auth_mod.AuthError):
        auth_mod.fetch_email("tok")
    _userinfo(monkeypatch, {"email": "a@x.edu"})
    with pytest.raises(auth_mod.AuthError):
        auth_mod.fetch_email("tok")


def test_fetch_email_normalizes(monkeypatch):
    _userinfo(monkeypatch, {"email": "Owner@X.edu", "email_verified": True})
    assert auth_mod.fetch_email("tok") == "owner@x.edu"


def test_default_secret_key_rejected_in_prod(monkeypatch):
    from pydantic import ValidationError

    from app.config import INSECURE_SECRET_KEY, Settings

    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("DEV", raising=False)
    # a plain VPS deploy: nothing says "production", but the key is public
    with pytest.raises(ValidationError, match="DEV=1"):
        Settings(_env_file=None, secret_key=INSECURE_SECRET_KEY)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key="")
    Settings(_env_file=None, secret_key=INSECURE_SECRET_KEY, dev=True)  # explicit dev
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key=INSECURE_SECRET_KEY, dev=True,
                 session_secure_cookie=True)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key=INSECURE_SECRET_KEY, dev=True)
    Settings(_env_file=None, secret_key="a-real-random-value")


def test_logout_is_csrf_checked_post(testapp):
    import re

    client = testapp["client"]
    assert client.get("/logout").status_code == 405
    assert client.post("/logout", data={"csrf_token": "bogus"}).status_code == 403
    page = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_cross_user_isolation(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        other = User(email="other@x.edu")
        s.add(other)
        s.commit()
        s.refresh(other)
        course = Course(user_id=other.id, source="moodle", source_id="cX",
                        name="Other course")
        s.add(course)
        s.commit()
        s.refresh(course)
        cid = str(course.id)
    assert client.get(f"/courses/{cid}").status_code == 404
    assert "Other course" not in client.get("/").text


def _callback_client(email: str, monkeypatch, **session_data):
    """TestClient with a signed session holding a pending state "s1" + mocked Google."""
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel

    from app.db import get_session

    engine = make_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override
    # a first sign-in: the consent screen hands out a refresh token
    monkeypatch.setattr(
        auth_mod, "exchange_code", lambda *a: {"access_token": "tok", "refresh_token": "rt"}
    )
    monkeypatch.setattr(auth_mod, "fetch_email", lambda tok: email)
    client = TestClient(app, follow_redirects=False)
    _set_session_cookie(client, _signed_session({"oauth_states": {"s1": False}, **session_data}))
    return client


def _signed_session(data: dict) -> str:
    import json
    from base64 import b64encode

    import itsdangerous

    signer = itsdangerous.TimestampSigner(str(settings.secret_key))
    return signer.sign(b64encode(json.dumps(data).encode()).decode()).decode()


def _set_session_cookie(client, value: str) -> None:
    # the domain the app's own Set-Cookie uses, so the two don't coexist
    client.cookies.set("session", value, domain="testserver.local")


def test_allowlist_blocks_stranger(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_emails", "owner@x.edu")
    client = _callback_client("stranger@x.com", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        _assert_login_failed(client, r, "stranger@x.com isn")
    finally:
        app.dependency_overrides.clear()


def test_allowlist_permits_owner(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_emails", "owner@x.edu")
    client = _callback_client("owner@x.edu", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303 and r.headers["location"] == "/"
    finally:
        app.dependency_overrides.clear()


def test_empty_allowlist_permits_anyone(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("anyone@x.com", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303
    finally:
        app.dependency_overrides.clear()


def test_domain_allowlist(monkeypatch):
    from app.main import email_allowed

    monkeypatch.setattr(settings, "allowed_emails", "@strathmore.edu, guest@x.com")
    assert email_allowed("student@strathmore.edu")
    assert email_allowed("guest@x.com")
    assert not email_allowed("other@x.com")
    assert not email_allowed("evil@notstrathmore.edu")
    assert not email_allowed("x@strathmore.edu.evil.com")


def test_default_allowlist_is_strathmore():
    from app.config import Settings

    assert Settings(_env_file=None).allowed_emails == "@strathmore.edu"


def test_domain_allowlist_blocks_outsider_at_callback(monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "@strathmore.edu")
    client = _callback_client("someone@gmail.com", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        _assert_login_failed(client, r, "use your university account")
    finally:
        app.dependency_overrides.clear()


def test_timezone_setting_must_be_an_iana_zone():
    from app.config import Settings

    assert str(Settings(_env_file=None).tz) == "Africa/Nairobi"
    with pytest.raises(ValueError, match="TIMEZONE"):
        Settings(_env_file=None, timezone="Mars/Olympus")


def test_login_url_prompts_consent_only_when_asked():
    url = auth_mod.login_url("cid", "https://x/cb", "s")
    assert "prompt=select_account" in url and "consent" not in url
    url = auth_mod.login_url("cid", "https://x/cb", "s", consent=True, login_hint="a@x.edu")
    assert "prompt=consent" in url and "login_hint=a%40x.edu" in url


def test_login_google_starts_with_account_chooser():
    from fastapi.testclient import TestClient

    client = TestClient(app, follow_redirects=False)
    r = client.get("/login/google")
    assert r.status_code == 303 and "prompt=select_account" in r.headers["location"]
    assert list(_session_data(client)["oauth_states"].values()) == [False]


def _state_of(location: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(location).query)["state"][0]


def test_double_tapped_sign_in_accepts_either_callback(monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("tap@x.edu", monkeypatch)
    try:
        first = _state_of(client.get("/login/google").headers["location"])
        second = _state_of(client.get("/login/google").headers["location"])
        assert first != second
        # Google returns the first tap; the second is still at Google
        r = client.get("/auth/callback", params={"code": "c", "state": first})
        assert r.status_code == 303 and r.headers["location"] == "/"
        data = _session_data(client)
        assert "user_id" in data and second in data["oauth_states"]
        assert first not in data["oauth_states"]
        # the second lands too, and signs in afresh
        r = client.get("/auth/callback", params={"code": "c2", "state": second})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert _session_data(client)["oauth_states"] == {"s1": False}
    finally:
        app.dependency_overrides.clear()


def test_oauth_state_is_single_use(monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("once@x.edu", monkeypatch)
    try:
        assert client.get("/auth/callback", params={"code": "c", "state": "s1"}).status_code == 303
        replay = TestClient(app, follow_redirects=False)
        _set_session_cookie(replay, _signed_session({"oauth_states": {}}))
        r = replay.get("/auth/callback", params={"code": "c", "state": "s1"})
        _assert_login_failed(replay, r, "sign-in link expired")
    finally:
        app.dependency_overrides.clear()


def test_stale_callback_after_sign_in_goes_home(monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("home@x.edu", monkeypatch)
    try:
        client.get("/auth/callback", params={"code": "c", "state": "s1"})
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303 and r.headers["location"] == "/"
    finally:
        app.dependency_overrides.clear()


def test_pending_oauth_states_are_bounded():
    from app.main import MAX_PENDING_OAUTH

    client = TestClient(app, follow_redirects=False)
    states = [_state_of(client.get("/login/google").headers["location"])
              for _ in range(MAX_PENDING_OAUTH + 2)]
    assert list(_session_data(client)["oauth_states"]) == states[-MAX_PENDING_OAUTH:]


def _no_refresh_token(monkeypatch):
    monkeypatch.setattr(auth_mod, "exchange_code", lambda *a: {"access_token": "tok"})


def test_returning_user_with_stored_token_signs_in_without_consent(monkeypatch):
    from app.db import get_session

    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("back@x.edu", monkeypatch)
    _no_refresh_token(monkeypatch)
    try:
        with next(app.dependency_overrides[get_session]()) as s:
            auth_mod.sign_in(s, "back@x.edu", "stored-rt")
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "user_id" in _session_data(client)
        with next(app.dependency_overrides[get_session]()) as s:
            assert auth_mod.refresh_token_for(auth_mod.find_user(s, "back@x.edu")) == "stored-rt"
    finally:
        app.dependency_overrides.clear()


def test_missing_refresh_token_reprompts_with_consent_once(monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("new@x.edu", monkeypatch)
    _no_refresh_token(monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        loc = r.headers["location"]
        assert r.status_code == 303 and loc.startswith(auth_mod.AUTH_URL)
        assert "prompt=consent" in loc and "login_hint=new%40x.edu" in loc
        data = _session_data(client)
        state = _state_of(loc)
        assert "user_id" not in data and data["oauth_states"] == {state: True}
        # the consent round still came back without one: sign in anyway
        r = client.get("/auth/callback", params={"code": "c2", "state": state})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "user_id" in _session_data(client)
    finally:
        app.dependency_overrides.clear()


def test_login_requests_drive_readonly():
    url = auth_mod.login_url("cid", "https://x/cb", "state123")
    assert "drive.readonly" in url


def test_sign_in_with_refresh_token_queues_classroom_sync(monkeypatch):
    from sqlmodel import select

    from app.config import settings
    from app.db import get_session
    from app.models import Job

    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("new@x.edu", monkeypatch)
    monkeypatch.setattr(auth_mod, "exchange_code",
                        lambda *a: {"access_token": "tok", "refresh_token": "rt"})
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303
        with next(app.dependency_overrides[get_session]()) as s:
            jobs = s.exec(select(Job)).all()
        assert [(j.type, j.payload) for j in jobs] == [
            ("sync", {"source": "classroom", "course_id": None, "user_email": "new@x.edu"})]
    finally:
        app.dependency_overrides.clear()


def test_enqueue_sync_once_skips_while_one_is_active():
    from app.jobs import enqueue_sync_once

    with _memory_session() as s:
        first = enqueue_sync_once(s, "classroom", "a@x.edu")
        assert first is not None
        assert enqueue_sync_once(s, "classroom", "a@x.edu") is None  # still pending
        assert enqueue_sync_once(s, "moodle", "a@x.edu") is not None  # other source
        assert enqueue_sync_once(s, "classroom", "b@x.edu") is not None  # other user
        first.status = "completed"
        s.add(first)
        s.commit()
        assert enqueue_sync_once(s, "classroom", "a@x.edu") is not None


def _session_data(client) -> dict:
    import json
    from base64 import b64decode

    import itsdangerous

    cookie = client.cookies.get("session")
    if cookie is None:  # an emptied session deletes its cookie
        return {}
    signer = itsdangerous.TimestampSigner(str(settings.secret_key))
    return json.loads(b64decode(signer.unsign(cookie)))


def _assert_login_failed(client, response, message: str):
    """Redirected to /login, the reason shown there, and not signed in."""
    assert response.status_code == 303 and response.headers["location"] == "/login"
    page = client.get("/login")
    assert page.status_code == 200 and message.lower() in page.text.lower()
    assert "user_id" not in _session_data(client)
    assert client.get("/").headers["location"] == "/login"


def test_callback_google_error_redirects_to_login(monkeypatch):
    client = _callback_client("owner@x.edu", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"error": "access_denied", "state": "s1"})
        _assert_login_failed(client, r, "sign-in was cancelled")
    finally:
        app.dependency_overrides.clear()


def test_callback_expired_code_redirects_to_login(monkeypatch):
    # Google rejects an expired or already-used code: that was a 500
    def rejected(*a):
        raise auth_mod.AuthError("token exchange failed: HTTP Error 400: invalid_grant")

    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("owner@x.edu", monkeypatch)
    monkeypatch.setattr(auth_mod, "exchange_code", rejected)
    try:
        r = client.get("/auth/callback", params={"code": "used", "state": "s1"})
        _assert_login_failed(client, r, "didn&#39;t complete")
    finally:
        app.dependency_overrides.clear()


def test_callback_without_access_token_redirects_to_login(monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("owner@x.edu", monkeypatch)
    monkeypatch.setattr(auth_mod, "exchange_code", lambda *a: {"error": "invalid_grant"})
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        _assert_login_failed(client, r, "didn&#39;t complete")
    finally:
        app.dependency_overrides.clear()


def test_callback_bad_state_redirects_to_login(monkeypatch):
    client = _callback_client("owner@x.edu", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "forged"})
        _assert_login_failed(client, r, "link expired")
    finally:
        app.dependency_overrides.clear()


def _signed_in_client(monkeypatch, email="owner@x.edu", **session_data):
    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client(email, monkeypatch, **session_data)
    r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200
    return client


def test_login_starts_a_fresh_session(monkeypatch):
    # a CSRF token (or anything else) planted before sign-in must not survive it
    try:
        client = _signed_in_client(monkeypatch, csrf_token="planted", flash={"x": 1})
        data = _session_data(client)
        assert data.get("csrf_token") != "planted" and "flash" not in data
        assert set(data) >= {"user_id", "session_version"}
    finally:
        app.dependency_overrides.clear()


def _csrf(client) -> str:
    import re

    return re.search(r'name="csrf_token" value="([^"]+)"', client.get("/").text).group(1)


def test_logout_revokes_copies_of_the_session(monkeypatch):
    try:
        client = _signed_in_client(monkeypatch)
        stolen = client.cookies["session"]
        stolen_csrf = _session_data(client)["csrf_token"]
        r = client.post("/logout", data={"csrf_token": _csrf(client)})
        assert r.status_code == 303
        thief = TestClient(app, follow_redirects=False)
        _set_session_cookie(thief, stolen)
        assert thief.get("/").headers["location"] == "/login"
        # signed in again; the dead copy must not be able to keep revoking
        _set_session_cookie(client, _signed_session({"oauth_states": {"s1": False}}))
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303 and client.get("/").status_code == 200
        _set_session_cookie(thief, stolen)
        r = thief.post("/logout", data={"csrf_token": stolen_csrf})
        assert r.status_code == 303  # passed CSRF, but revoked nothing
        assert client.get("/").status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_removed_from_allowlist_ends_session(monkeypatch):
    try:
        client = _signed_in_client(monkeypatch, email="leaver@x.edu")
        monkeypatch.setattr(settings, "allowed_emails", "@strathmore.edu")
        r = client.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "user_id" not in _session_data(client)
        assert client.get("/login").status_code == 200  # no redirect loop
    finally:
        app.dependency_overrides.clear()


def test_revoke_sessions_ends_existing_sessions(monkeypatch):
    from app.db import get_session

    try:
        client = _signed_in_client(monkeypatch)
        with next(app.dependency_overrides[get_session]()) as s:
            auth_mod.revoke_sessions(s, auth_mod.find_user(s, "owner@x.edu"))
        assert client.get("/").headers["location"] == "/login"
    finally:
        app.dependency_overrides.clear()


def test_overlapping_revocations_both_count(tmp_path):
    # two revocations load the same session_version N; a read-modify-write
    # would leave N+1 and keep alive a cookie issued between the commits
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(email="owner@x.edu"))
        s.commit()
    with Session(engine) as a, Session(engine) as b:
        user_a = auth_mod.find_user(a, "owner@x.edu")
        user_b = auth_mod.find_user(b, "owner@x.edu")
        assert user_a.session_version == user_b.session_version == 0
        auth_mod.revoke_sessions(a, user_a)
        auth_mod.revoke_sessions(b, user_b)
    with Session(engine) as s:
        assert auth_mod.find_user(s, "owner@x.edu").session_version == 2


def test_session_has_explicit_max_age():
    from starlette.middleware.sessions import SessionMiddleware

    mw = next(m for m in app.user_middleware if m.cls is SessionMiddleware)
    assert mw.kwargs["max_age"] == settings.session_max_age == 7 * 24 * 3600


def test_concurrent_first_sign_in_updates_the_winner(monkeypatch):
    # two first logins race: both miss the lookup, the loser's INSERT hits
    # the unique email constraint; it must update the winner's row, not 500
    real_find = auth_mod.find_user
    calls = []

    def racing_find(session, email):
        calls.append(email)
        if len(calls) == 1:
            session.add(User(email=email))  # the other request's row
            session.commit()
            return None
        return real_find(session, email)

    with _memory_session() as s:
        monkeypatch.setattr(auth_mod, "find_user", racing_find)
        user = auth_mod.sign_in(s, "race@x.edu", refresh_token="rt")
        assert len(s.exec(select(User)).all()) == 1
        assert auth_mod.refresh_token_for(user) == "rt" and len(calls) == 2


def test_oauth_redirect_uri_comes_from_app_base_url(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://study.example.edu/")
    client = TestClient(app, follow_redirects=False)
    r = client.get("/login/google", headers={"Host": "evil.example.com"})
    assert "redirect_uri=https%3A%2F%2Fstudy.example.edu%2Fauth%2Fcallback" in r.headers["location"]
    assert "evil" not in r.headers["location"]


def test_callback_exchanges_with_app_base_url_redirect(monkeypatch):
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "app_base_url", "https://study.example.edu")
    client = _callback_client("new@x.edu", monkeypatch)
    seen = []
    monkeypatch.setattr(auth_mod, "exchange_code",
                        lambda cid, secret, code, uri: seen.append(uri) or {"access_token": "t"})
    try:
        assert client.get("/auth/callback", params={"code": "c", "state": "s1"}).status_code == 303
        assert seen == ["https://study.example.edu/auth/callback"]
    finally:
        app.dependency_overrides.clear()


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _user_with_google_token(s):
    from app.crypto import seal

    user = User(email="g@x.edu", google_refresh_token=seal("google-refresh-token", "rt-1"))
    s.add(user)
    s.commit()
    s.refresh(user)
    return user


@pytest.mark.parametrize("outcome, revoked", [
    ("ok", True), ("invalid_token", True), ("unreachable", False)])
def test_revoke_google_access_always_forgets_the_token(monkeypatch, outcome, revoked):
    import urllib.error

    sent = []

    def fake_urlopen(req, timeout):
        sent.append((req.full_url, req.data))
        if outcome == "invalid_token":
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, None)
        if outcome == "unreachable":
            raise urllib.error.URLError("down")
        return _Resp()

    monkeypatch.setattr(auth_mod.urllib.request, "urlopen", fake_urlopen)
    engine = make_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = _user_with_google_token(s)
        assert auth_mod.revoke_google_access(s, user) is revoked
        s.refresh(user)
        assert user.google_refresh_token is None
    assert sent == [(auth_mod.REVOKE_URL, b"token=rt-1")]


def test_revoke_without_a_token_skips_google(monkeypatch):
    monkeypatch.setattr(auth_mod.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("called Google"))
    engine = make_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="none@x.edu")
        s.add(user)
        s.commit()
        assert auth_mod.revoke_google_access(s, user) is True


def test_settings_revoke_google_button(testapp, monkeypatch):
    import re

    from app.crypto import seal

    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        user.google_refresh_token = seal("google-refresh-token", "rt-1")
        s.add(user)
        s.commit()
    monkeypatch.setattr(auth_mod.urllib.request, "urlopen", lambda req, timeout: _Resp())
    client = testapp["client"]
    page = client.get("/settings/moodle").text
    title = re.search(r"<title>(.*?)</title>", page, re.S).group(1)
    assert title == "Settings — Strathmore Study Buddy"
    assert page.count("Revoke Google access") == 1
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = client.post("/settings/google/disconnect", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/moodle"
    with testapp["Session"]() as s:
        assert s.get(User, testapp["user_id"]).google_refresh_token is None
    page = client.get("/settings/moodle").text
    assert "Google access revoked" in page and "Revoke Google access" not in page
