"""Attempt limits that every web instance shares.

The general POST limiter (app.security.HitTable) lives in process memory,
which is fine for a coarse abuse guard. A password limit isn't: on Cloud Run
each instance would keep its own count, and scale-to-zero would reset it, so
parallel requests or a short wait buy fresh guesses. These counts live in
Postgres (login_attempts) instead.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlmodel import Session, delete, func, select

from app.models import LoginAttempt


def _digest(key: str) -> str:
    """What is stored: a hash, so the table holds no Moodle usernames."""
    return hashlib.sha256(key.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo: assume UTC for naive datetimes."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def attempt(session: Session, keys, limit: int, window: float,
            now: datetime | None = None) -> float | None:
    """Count one attempt against each of `keys`, or, when any key already has
    `limit` attempts in the last `window` seconds, return the seconds until
    the oldest of them ages out and count nothing. Commits.

    On Postgres each key is locked until commit, so concurrent attempts on
    one key are counted one after another and can't overspend the last one.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(seconds=window)
    digests = sorted({_digest(k) for k in keys})  # sorted: no lock-order deadlock
    if session.get_bind().dialect.name == "postgresql":
        from app.pipeline import _lock_key

        for d in digests:
            session.exec(text("SELECT pg_advisory_xact_lock(:k)").bindparams(
                k=_lock_key(f"app.throttle:{d}")))
    # old attempts never matter again: drop them (any key's, so a username
    # tried once doesn't stay on record forever)
    session.exec(delete(LoginAttempt).where(LoginAttempt.attempted_at <= since))
    waits = []
    for d in digests:
        count, oldest = session.exec(
            select(func.count(LoginAttempt.id), func.min(LoginAttempt.attempted_at))
            .where(LoginAttempt.key == d, LoginAttempt.attempted_at > since)
        ).one()
        if count >= limit:
            waits.append((_aware(oldest) - since).total_seconds())
    if not waits:
        session.add_all(LoginAttempt(key=d, attempted_at=now) for d in digests)
    session.commit()
    return max(waits) if waits else None
