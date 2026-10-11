"""Account deletion (app.account, Settings -> Delete account, admin_cli)."""

import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from app import account
from app import auth as auth_mod
from app.crypto import seal
from app.models import (
    Assignment,
    Chunk,
    Course,
    ItemFlag,
    Job,
    NotificationEvent,
    QuizAttempt,
    QuizFailure,
    QuizItem,
    Resource,
    ReviewLog,
    ReviewState,
    Topic,
    User,
)
from tests.dbutil import make_engine

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
TABLES = (User, Course, Topic, Resource, Assignment, Chunk, QuizItem, QuizAttempt,
          QuizFailure, ReviewState, ItemFlag, ReviewLog, NotificationEvent, Job)


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def google(monkeypatch):
    """Tokens Google was asked to revoke; set "down" to make it unreachable."""
    state = {"revoked": [], "down": False}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout):
        if state["down"]:
            raise OSError("unreachable")
        state["revoked"].append(req.data)
        return Resp()

    monkeypatch.setattr(auth_mod.urllib.request, "urlopen", fake_urlopen)
    return state


def _add(session, row):
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _populate(session, user: User, tag: str) -> None:
    """One of everything a user can own, over the same material for each."""
    course = _add(session, Course(user_id=user.id, source="moodle", source_id="c1",
                                  name="Data Structures"))
    topic = _add(session, Topic(course_id=course.id, source_id="t1", title="Trees"))
    session.add(Assignment(course_id=course.id, topic_id=topic.id, source_id="a1",
                           title="Lab 1"))
    resource = _add(session, Resource(
        topic_id=topic.id, source="moodle", source_id="r1", type="file",
        title="Slides", extracted_text="Trees branch.", content_hash="same-file",
        status="extracted"))
    chunk = _add(session, Chunk(resource_id=resource.id, title="C0",
                                content="Trees branch."))
    item = _add(session, QuizItem(
        chunk_id=chunk.id, question="What branches?", question_type="short_answer",
        correct_answer="trees", generation_key=f"{chunk.id}:1"))
    session.add(QuizAttempt(chunk_id=chunk.id, attempt=1))
    session.add(QuizFailure(chunk_id=chunk.id, attempt=2, failures=1))
    session.add(ReviewState(user_id=user.id, quiz_item_id=item.id, next_review_date=NOW))
    session.add(ItemFlag(user_id=user.id, quiz_item_id=item.id, suspended_at=NOW))
    session.add(ReviewLog(user_id=user.id, quiz_item_id=item.id, verdict="correct",
                          answered_at=NOW))
    session.add(NotificationEvent(user_id=user.id, type="review_due", payload={"n": 1}))
    for status in ("pending", "completed"):
        session.add(Job(type="sync", status=status, payload={
            "source": "moodle", "user_email": user.email, "course_id": tag + status}))
    session.commit()


def _counts(session) -> dict:
    return {t.__tablename__: len(session.exec(select(t)).all()) for t in TABLES}


def _two_users(session):
    leaver = _add(session, User(
        email="leaver@x.edu", moodle_token="sealed",
        google_refresh_token=seal("google-refresh-token", "rt-1")))
    stayer = _add(session, User(email="stayer@x.edu"))
    _populate(session, leaver, "l")
    _populate(session, stayer, "s")
    return leaver, stayer


def test_delete_account_removes_only_that_users_rows(session, google):
    leaver, stayer = _two_users(session)
    stayer_id = stayer.id
    # a pre-sign-in course nobody owns is nobody's to delete
    session.add(Course(user_id=None, source="moodle", source_id="legacy", name="Legacy"))
    session.add(Job(type="send_notifications", payload={}))
    session.commit()
    before = _counts(session)

    assert account.delete_account(session, leaver) is True

    assert google["revoked"] == [b"token=rt-1"]
    after = _counts(session)
    # the stayer's copy of the same file, and everything built from it, stays
    assert after == {**{name: n // 2 for name, n in before.items()},
                     "courses": 2, "jobs": 3}
    assert session.exec(select(User.email)).all() == ["stayer@x.edu"]
    for table in (ReviewState, ItemFlag, ReviewLog, NotificationEvent):
        assert session.exec(select(table.user_id)).all() == [stayer_id]
    assert {c.user_id for c in session.exec(select(Course)).all()} == {stayer_id, None}
    emails = [j.payload.get("user_email") for j in session.exec(select(Job)).all()]
    assert sorted(emails, key=str) == [None, "stayer@x.edu", "stayer@x.edu"]


def test_delete_account_goes_ahead_when_google_is_unreachable(session, google):
    leaver, _ = _two_users(session)
    google["down"] = True
    assert account.delete_account(session, leaver) is False
    assert session.exec(select(User.email)).all() == ["stayer@x.edu"]


def test_delete_account_of_a_user_with_nothing(session, google):
    user = _add(session, User(email="new@x.edu"))
    assert account.delete_account(session, user) is True
    assert google["revoked"] == []  # no grant stored, nothing to revoke
    assert session.exec(select(User)).all() == []


def _busy(monkeypatch, busy_source: str):
    """Make the user's `busy_source` sync look like it is running."""
    asked = []

    @contextmanager
    def fake_lock(source, email, bind):
        asked.append((source, email))
        yield source != busy_source

    monkeypatch.setattr(account, "sync_lock", fake_lock)
    return asked


@pytest.mark.parametrize("busy_source", ["moodle", "classroom"])
def test_delete_account_waits_for_a_running_sync(session, google, monkeypatch, busy_source):
    leaver, _ = _two_users(session)
    asked = _busy(monkeypatch, busy_source)
    before = _counts(session)
    with pytest.raises(account.SyncRunning):
        account.delete_account(session, leaver)
    assert (busy_source, "leaver@x.edu") in asked
    # nothing half-done: the grant is still theirs too
    assert _counts(session) == before and google["revoked"] == []
    session.refresh(leaver)
    assert leaver.google_refresh_token is not None


def test_sync_job_and_deletion_share_a_lock(session, monkeypatch):
    """The sync handler must take the lock deletion checks, or a sync could
    write courses for a user being deleted."""
    from app import jobs

    names = []

    @contextmanager
    def fake_advisory_lock(name, bind=None, wait=False):
        names.append(name)
        yield False

    monkeypatch.setattr("app.pipeline.advisory_lock", fake_advisory_lock)
    user = _add(session, User(email="leaver@x.edu"))
    with pytest.raises(jobs.Defer):
        jobs.run_sync_job(session, {"source": "moodle", "user_email": user.email})
    with pytest.raises(account.SyncRunning):
        account.delete_account(session, user)
    assert names == ["app.sync:moodle:leaver@x.edu"] * 2


# -- Settings -> Delete account ------------------------------------------


def _csrf(client) -> str:
    page = client.get("/settings/moodle").text
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def _delete(client, **data):
    return client.post("/settings/account/delete", data=data, follow_redirects=False)


def test_settings_offers_deletion_behind_typing_the_address(testapp):
    client = testapp["client"]
    page = client.get("/settings/moodle").text
    assert page.count('action="/settings/account/delete"') == 1
    assert "Type <strong>test@x.edu</strong> to confirm" in page

    assert _delete(client, csrf_token="bogus", confirm_email="test@x.edu").status_code == 403
    for typed in ("", "someone-else@x.edu"):
        r = _delete(client, csrf_token=_csrf(client), confirm_email=typed)
        assert r.status_code == 303 and r.headers["location"] == "/settings/moodle"
        assert "Nothing was deleted" in client.get("/settings/moodle").text
    with testapp["Session"]() as s:
        assert s.get(User, testapp["user_id"]) is not None


def test_deleting_from_settings_signs_out_and_says_so(testapp):
    client = testapp["client"]
    with testapp["Session"]() as s:
        _populate(s, s.get(User, testapp["user_id"]), "t")
    r = _delete(client, csrf_token=_csrf(client), confirm_email=" Test@X.edu ")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    with testapp["Session"]() as s:
        assert set(_counts(s).values()) == {0}
    page = client.get("/login")
    assert page.status_code == 200  # not bounced to "/": the session is gone
    assert "have been deleted" in page.text and "notice-success" in page.text
    assert "have been deleted" not in client.get("/login").text  # shown once


def test_deleting_from_settings_warns_when_google_was_unreachable(testapp, google):
    client = testapp["client"]
    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        user.google_refresh_token = seal("google-refresh-token", "rt-1")
        s.add(user)
        s.commit()
    google["down"] = True
    r = _delete(client, csrf_token=_csrf(client), confirm_email="test@x.edu")
    assert r.headers["location"] == "/login"
    page = client.get("/login").text
    assert "myaccount.google.com/permissions" in page and "notice-warning" in page
    with testapp["Session"]() as s:
        assert s.get(User, testapp["user_id"]) is None


def test_deleting_from_settings_during_a_sync_deletes_nothing(testapp, monkeypatch):
    client = testapp["client"]
    _busy(monkeypatch, "moodle")
    r = _delete(client, csrf_token=_csrf(client), confirm_email="test@x.edu")
    assert r.status_code == 303 and r.headers["location"] == "/settings/moodle"
    assert "syncing right now" in client.get("/settings/moodle").text
    with testapp["Session"]() as s:
        assert s.get(User, testapp["user_id"]) is not None


def test_signing_in_after_deletion_starts_an_empty_account(session, google):
    leaver, _ = _two_users(session)
    account.delete_account(session, leaver)
    again = auth_mod.sign_in(session, "leaver@x.edu", "rt-2")
    assert again.moodle_token is None
    assert session.exec(select(Course).where(Course.user_id == again.id)).all() == []


# -- admin_cli delete-user -----------------------------------------------


def _run_cli(monkeypatch, session, *argv):
    from app import admin_cli

    monkeypatch.setattr(admin_cli, "engine", session.get_bind())
    monkeypatch.setattr(sys, "argv", ["admin_cli", *argv])
    admin_cli.main()


def test_admin_cli_delete_user(session, google, monkeypatch, capsys):
    _two_users(session)
    _run_cli(monkeypatch, session, "delete-user", "Leaver@x.edu")
    assert "deleted leaver@x.edu and all their data" in capsys.readouterr().out
    session.expire_all()
    assert session.exec(select(User.email)).all() == ["stayer@x.edu"]
    assert len(session.exec(select(Course)).all()) == 1
    assert google["revoked"] == [b"token=rt-1"]


def test_admin_cli_delete_user_reports_what_it_could_not_do(
        session, google, monkeypatch, capsys):
    _two_users(session)
    with pytest.raises(SystemExit, match="no such user"):
        _run_cli(monkeypatch, session, "delete-user", "nobody@x.edu")
    _busy(monkeypatch, "classroom")
    with pytest.raises(SystemExit, match="nothing deleted"):
        _run_cli(monkeypatch, session, "delete-user", "leaver@x.edu")
    session.expire_all()
    assert len(session.exec(select(User)).all()) == 2
