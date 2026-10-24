"""Incident response: `app.auth.lock_down` and `admin_cli lockdown`.

Covers the leaked-SECRET_KEY case: every session ends (a cookie forged with
the leaked key stops working), every stored credential is forgotten, and the
Google grants are revoked with Google *before* the key is rotated.
"""

import sys

import pytest
from sqlmodel import Session, SQLModel, select

from app import auth as auth_mod
from app.config import settings
from app.crypto import seal
from app.models import User
from tests.dbutil import make_engine

GOOGLE_PURPOSE = "google-refresh-token"


@pytest.fixture(autouse=True)
def _no_shared_tokens(monkeypatch):
    """A developer's .env may set these; tests opt in explicitly."""
    monkeypatch.setattr(settings, "google_refresh_token", "")
    monkeypatch.setattr(settings, "moodle_token", "")


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def google(monkeypatch):
    """Tokens Google was asked to revoke; set "down" to make it unreachable."""
    state = {"revoked": [], "down": False, "timeouts": []}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout):
        state["timeouts"].append(timeout)
        if state["down"]:
            raise OSError("unreachable")
        state["revoked"].append(req.data)
        return Resp()

    monkeypatch.setattr(auth_mod.urllib.request, "urlopen", fake_urlopen)
    return state


def _users(session):
    session.add(
        User(
            email="a@x.edu",
            google_refresh_token=seal(GOOGLE_PURPOSE, "rt-a"),
            moodle_token="v1:sealed-moodle",
        )
    )
    session.add(User(email="b@x.edu", moodle_token="v1:sealed-moodle"))
    session.add(User(email="c@x.edu"))  # nothing stored, still signed out
    session.commit()


def _run_cli(monkeypatch, session, *argv):
    from app import admin_cli

    monkeypatch.setattr(admin_cli, "engine", session.get_bind())
    monkeypatch.setattr(sys, "argv", ["admin_cli", *argv])
    admin_cli.main()


def test_lock_down_signs_everyone_out_and_forgets_every_credential(session, google):
    _users(session)
    report = auth_mod.lock_down(session)

    assert report == auth_mod.LockdownReport(
        users=2, cleared=2, google_revoked=1, google_failed=0, undecryptable=0, moodle_cleared=2
    )
    session.expire_all()
    assert all(u.session_version == 1 for u in session.exec(select(User)).all())
    assert [u.email for u in session.exec(select(User).where(User.moodle_token.is_not(None)))] == []
    assert [
        u.email for u in session.exec(select(User).where(User.google_refresh_token.is_not(None)))
    ] == []
    assert google["revoked"] == [b"token=rt-a"]


def test_lock_down_bumps_an_already_revoked_session_again(session):
    _users(session)
    user = session.exec(select(User).where(User.email == "a@x.edu")).one()
    auth_mod.revoke_sessions(session, user)  # a previous, narrower revocation
    before = {u.email: u.session_version for u in session.exec(select(User)).all()}

    auth_mod.lock_down(session, revoke_google=False)

    session.expire_all()
    after = {u.email: u.session_version for u in session.exec(select(User)).all()}
    # every user lands one past where they were: no cookie issued before the
    # lockdown (including one forged with the leaked key) survives it
    assert after == {email: version + 1 for email, version in before.items()}
    assert after["a@x.edu"] == 2


def test_lock_down_kills_sessions_before_touching_credentials(session, google, monkeypatch):
    # even if the sweep dies (DB error, bug), no pre-incident cookie survives
    _users(session)

    def boom(user):
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(auth_mod, "refresh_token_for", boom)
    with pytest.raises(RuntimeError, match="sweep failed"):
        auth_mod.lock_down(session)

    session.expire_all()
    assert all(u.session_version == 1 for u in session.exec(select(User)).all())


def test_lock_down_counts_the_grants_no_google_revoke_leaves_live(session, google):
    _users(session)
    report = auth_mod.lock_down(session, revoke_google=False)

    assert report.google_skipped == 1
    assert report.google_revoked == 0 and report.google_failed == 0
    assert google["revoked"] == []
    # only revocation was skipped: the stored token is still forgotten here
    session.expire_all()
    assert session.exec(select(User.google_refresh_token)).all() == [None, None, None]


def test_lock_down_still_clears_when_google_is_unreachable(session, google):
    _users(session)
    google["down"] = True
    report = auth_mod.lock_down(session)

    assert report.google_failed == 1 and report.google_revoked == 0
    assert report.cleared == 2
    session.expire_all()
    assert session.exec(select(User.google_refresh_token)).all() == [None, None, None]


def test_lock_down_after_secret_rotation_reports_undecryptable(session, google, monkeypatch):
    session.add(User(email="a@x.edu", google_refresh_token=seal(GOOGLE_PURPOSE, "rt-a")))
    session.commit()
    monkeypatch.setattr(auth_mod.settings, "secret_key", "rotated")

    report = auth_mod.lock_down(session)

    assert report.undecryptable == 1
    assert report.cleared == 1
    assert google["revoked"] == []  # nothing decryptable to revoke with
    session.expire_all()
    assert session.exec(select(User.google_refresh_token)).all() == [None]


def test_lock_down_uses_a_short_timeout_per_grant(session, google):
    _users(session)
    auth_mod.lock_down(session)
    assert google["timeouts"] == [10]


def test_lock_down_can_keep_moodle_tokens(session, google):
    _users(session)
    report = auth_mod.lock_down(session, keep_moodle=True)

    assert report.cleared == 1  # only the user with a Google token
    session.expire_all()
    assert len(session.exec(select(User).where(User.moodle_token.is_not(None))).all()) == 2


def test_lock_down_dry_run_changes_nothing(session, google):
    _users(session)
    report = auth_mod.lock_down(session, dry_run=True)

    assert report.cleared == 2 and report.users == 2
    assert google["revoked"] == []
    session.expire_all()
    assert all(u.session_version == 0 for u in session.exec(select(User)).all())
    assert len(session.exec(select(User).where(User.moodle_token.is_not(None))).all()) == 2


def test_lock_down_no_op_on_an_empty_database(session, google):
    report = auth_mod.lock_down(session)
    assert report == auth_mod.LockdownReport(
        users=0, cleared=0, google_revoked=0, google_failed=0, undecryptable=0
    )


# -- the shared GOOGLE_REFRESH_TOKEN / MOODLE_TOKEN fallbacks --------------


def test_lock_down_revokes_the_shared_google_token(session, google, monkeypatch):
    monkeypatch.setattr(settings, "google_refresh_token", "shared-rt")
    report = auth_mod.lock_down(session)

    assert report.shared_google is True
    assert report.shared_google_revoked == 1
    assert report.shared_google_failed == 0
    assert google["revoked"] == [b"token=shared-rt"]


def test_lock_down_reports_a_shared_google_token_it_could_not_revoke(session, google, monkeypatch):
    monkeypatch.setattr(settings, "google_refresh_token", "shared-rt")
    google["down"] = True
    report = auth_mod.lock_down(session)

    assert report.shared_google is True
    assert report.shared_google_revoked == 0
    assert report.shared_google_failed == 1


def test_lock_down_dry_run_does_not_revoke_the_shared_google_token(session, google, monkeypatch):
    monkeypatch.setattr(settings, "google_refresh_token", "shared-rt")
    report = auth_mod.lock_down(session, dry_run=True)

    assert report.shared_google is True and report.shared_google_revoked == 0
    assert google["revoked"] == []


def test_lock_down_no_google_revoke_leaves_the_shared_grant_live(session, google, monkeypatch):
    monkeypatch.setattr(settings, "google_refresh_token", "shared-rt")
    report = auth_mod.lock_down(session, revoke_google=False)

    assert report.shared_google is True and report.shared_google_revoked == 0
    assert google["revoked"] == []


def test_lock_down_reports_the_shared_moodle_token(session, google, monkeypatch):
    monkeypatch.setattr(settings, "moodle_token", "shared-moodle")
    report = auth_mod.lock_down(session)
    assert report.shared_moodle is True


# -- admin_cli lockdown ---------------------------------------------------


def test_admin_cli_lockdown_reports_what_it_did(session, google, monkeypatch, capsys):
    _users(session)
    # Google is fully revoked, but the forgotten Moodle tokens are still live
    # at Moodle: automation must not read that as complete containment
    with pytest.raises(SystemExit) as exit_:
        _run_cli(monkeypatch, session, "lockdown")
    assert exit_.value.code == 1
    out = capsys.readouterr().out
    assert "signed out every user; forgot credentials for 2" in out
    assert "google grants revoked: 1" in out
    assert "2 Moodle token(s) were forgotten here but are still valid at Moodle" in out
    assert "lockdown is NOT complete" in out


def test_admin_cli_lockdown_succeeds_once_moodle_revocation_is_confirmed(
    session, google, monkeypatch, capsys
):
    _users(session)
    _run_cli(monkeypatch, session, "lockdown", "--moodle-revoked")  # exit 0
    out = capsys.readouterr().out
    assert "google grants revoked: 1" in out
    assert "lockdown is NOT complete" not in out


def test_admin_cli_lockdown_fails_on_the_shared_moodle_token(session, google, monkeypatch, capsys):
    monkeypatch.setattr(settings, "moodle_token", "shared-moodle")
    with pytest.raises(SystemExit) as exit_:
        _run_cli(monkeypatch, session, "lockdown")
    assert exit_.value.code == 1
    assert "lockdown is NOT complete" in capsys.readouterr().out
    _run_cli(monkeypatch, session, "lockdown", "--moodle-revoked")  # exit 0


def test_admin_cli_lockdown_keep_moodle_fails_without_confirmation(
    session, google, monkeypatch, capsys
):
    session.add(User(email="m@x.edu", moodle_token="v1:sealed-moodle"))
    session.commit()
    with pytest.raises(SystemExit) as exit_:
        _run_cli(monkeypatch, session, "lockdown", "--keep-moodle")
    assert exit_.value.code == 1
    assert "1 stored Moodle token(s) were left in place" in capsys.readouterr().out
    _run_cli(monkeypatch, session, "lockdown", "--keep-moodle", "--moodle-revoked")


def test_admin_cli_lockdown_without_moodle_tokens_needs_no_confirmation(
    session, google, monkeypatch
):
    session.add(User(email="c@x.edu"))
    session.commit()
    _run_cli(monkeypatch, session, "lockdown")  # nothing live at Moodle: exit 0


def test_lock_down_counts_the_moodle_tokens_it_leaves_live(session, google):
    _users(session)
    assert auth_mod.lock_down(session, dry_run=True).moodle_cleared == 2
    assert auth_mod.lock_down(session, keep_moodle=True).moodle_kept == 2
    assert auth_mod.lock_down(session).moodle_cleared == 2


def test_admin_cli_lockdown_warns_and_fails_on_undecryptable_tokens(
    session, google, monkeypatch, capsys
):
    session.add(User(email="a@x.edu", google_refresh_token=seal(GOOGLE_PURPOSE, "rt-a")))
    session.commit()
    monkeypatch.setattr(auth_mod.settings, "secret_key", "rotated")
    # the grant behind the unreadable token is still live at Google, so this is
    # not complete containment even though no revocation was attempted
    with pytest.raises(SystemExit) as exit_:
        _run_cli(monkeypatch, session, "lockdown")
    assert exit_.value.code == 1
    out = capsys.readouterr().out
    assert "did not decrypt" in out and "delete the OAuth client" in out


def test_admin_cli_lockdown_dry_run_says_so(session, google, monkeypatch, capsys):
    _users(session)
    _run_cli(monkeypatch, session, "lockdown", "--dry-run")
    assert (
        "dry run: would sign out every user and forget credentials for 2" in capsys.readouterr().out
    )
    session.expire_all()
    assert all(u.session_version == 0 for u in session.exec(select(User)).all())


def test_admin_cli_lockdown_no_google_revoke_and_keep_moodle(session, google, monkeypatch, capsys):
    _users(session)
    # the stored grant is forgotten here but still live at Google, so a
    # lockdown that was meant to contain it must not report success
    with pytest.raises(SystemExit) as exit_:
        _run_cli(monkeypatch, session, "lockdown", "--no-google-revoke", "--keep-moodle")
    assert exit_.value.code == 1
    out = capsys.readouterr().out
    assert google["revoked"] == []
    assert "forgot credentials for 1" in out
    assert "1 stored Google token(s) were not revoked" in out
    assert "still valid at Moodle" not in out
    assert "2 stored Moodle token(s) were left in place" in out
    session.expire_all()
    assert len(session.exec(select(User).where(User.moodle_token.is_not(None))).all()) == 2


def test_admin_cli_lockdown_dry_run_with_no_google_revoke_promises_no_revocation(
    session, google, monkeypatch, capsys
):
    _users(session)
    monkeypatch.setattr(auth_mod.settings, "google_refresh_token", "shared-rt")
    _run_cli(monkeypatch, session, "lockdown", "--dry-run", "--no-google-revoke")
    out = capsys.readouterr().out
    assert "would revoke it with Google" not in out
    assert (
        "GOOGLE_REFRESH_TOKEN is configured as a shared fallback and was not "
        "revoked (--no-google-revoke)" in out
    )
    assert google["revoked"] == []
    # a dry run reports, it doesn't fail automation and changes nothing
    session.expire_all()
    assert all(u.session_version == 0 for u in session.exec(select(User)).all())
