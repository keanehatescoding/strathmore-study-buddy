"""app.throttle: password-attempt limits kept in the database (#147)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from app import throttle
from app.models import LoginAttempt
from tests.dbutil import TEST_DATABASE_URL, make_engine

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _engine():
    engine = make_engine()
    SQLModel.metadata.create_all(engine)
    return engine


def test_limit_is_shared_across_sessions():
    # every web instance has its own sessions and memory: only what's in
    # the database is shared, so a fresh session must see earlier attempts
    engine = _engine()
    for n in range(3):
        with Session(engine) as s:
            assert throttle.attempt(s, ["moodle:victim"], 3, 900, T0 + timedelta(seconds=n)) is None
    with Session(engine) as s:
        wait = throttle.attempt(s, ["moodle:victim"], 3, 900, T0 + timedelta(seconds=10))
    assert wait == pytest.approx(890)  # until the first attempt ages out


def test_blocked_attempts_are_not_counted_and_old_ones_age_out():
    engine = _engine()
    with Session(engine) as s:
        assert throttle.attempt(s, ["k"], 1, 60, T0) is None
        assert throttle.attempt(s, ["k"], 1, 60, T0 + timedelta(seconds=30)) == pytest.approx(30)
        # the blocked try didn't extend the window: free again at T0 + 60
        assert throttle.attempt(s, ["k"], 1, 60, T0 + timedelta(seconds=61)) is None
        assert len(s.exec(select(LoginAttempt)).all()) == 1  # the expired one was pruned


def test_any_key_over_its_limit_blocks_and_counts_none():
    engine = _engine()
    with Session(engine) as s:
        assert throttle.attempt(s, ["user:1", "moodle:a"], 1, 900, T0) is None
        assert throttle.attempt(s, ["user:1", "moodle:b"], 1, 900, T0) is not None
        # moodle:b wasn't counted by the blocked attempt
        assert throttle.attempt(s, ["user:2", "moodle:b"], 1, 900, T0) is None


def test_keys_are_stored_hashed():
    engine = _engine()
    with Session(engine) as s:
        throttle.attempt(s, ["moodle:student123"], 5, 900, T0)
        [row] = s.exec(select(LoginAttempt)).all()
    assert "student123" not in row.key and len(row.key) == 64


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="advisory locks are Postgres-only")
def test_concurrent_attempts_cannot_overspend_the_last_one():
    import threading

    engine = _engine()
    holder = Session(engine)
    # an attempt that has taken the key's lock but not yet committed
    from sqlalchemy import text

    from app.pipeline import _lock_key

    holder.exec(text("SELECT pg_advisory_xact_lock(:k)").bindparams(
        k=_lock_key(f"app.throttle:{throttle._digest('moodle:victim')}")))
    holder.add(LoginAttempt(key=throttle._digest("moodle:victim"), attempted_at=T0))
    holder.flush()
    result = []

    def racer():
        with Session(engine) as s:
            result.append(throttle.attempt(s, ["moodle:victim"], 1, 900, T0))

    t = threading.Thread(target=racer)
    t.start()
    t.join(0.5)
    assert t.is_alive()  # waits for the first attempt to commit
    holder.commit()
    holder.close()
    t.join(10)
    assert result and result[0] is not None  # sees it, and is refused
