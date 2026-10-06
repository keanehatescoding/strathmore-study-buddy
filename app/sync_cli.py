"""Live sync entrypoint: python -m app.sync_cli --source moodle --user EMAIL [--course ID]
                       python -m app.sync_cli --source moodle --all-users --enqueue

Moodle uses the user's own connected token (Settings → Moodle), falling back
to MOODLE_TOKEN only for MOODLE_TOKEN_OWNER (see app.moodle_tokens).
Classroom reuses the Google refresh token stored on the user row at login,
falling back to GOOGLE_REFRESH_TOKEN only for GOOGLE_REFRESH_TOKEN_OWNER.
"""

from __future__ import annotations

import argparse
import sys

from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import User


class NotConnectedError(RuntimeError):
    """The user has no credentials for this source (a job failure, not a crash)."""


def build_adapter(source: str, user: User):
    if source == "moodle":
        from app.moodle import MoodleAdapter, MoodleClient
        from app.moodle_tokens import token_for

        token = token_for(user)
        if not token:
            raise NotConnectedError(
                f"{user.email} has not connected Moodle — use Settings → Moodle"
            )
        return MoodleAdapter(MoodleClient(settings.moodle_base_url, token))
    if source == "classroom":
        from app.auth import classroom_token_for
        from app.classroom import ClassroomAdapter, ClassroomClient, build_service

        refresh = classroom_token_for(user)
        if not refresh:
            raise NotConnectedError("no classroom refresh token — log in via Google first")
        service = build_service(
            settings.google_client_id, settings.google_client_secret, refresh
        )
        return ClassroomAdapter(ClassroomClient(service))
    raise ValueError(f"unknown source {source!r}")


def has_credentials(source: str, user: User) -> bool:
    if source == "moodle":
        from app.moodle_tokens import token_for

        return token_for(user) is not None
    from app.auth import classroom_token_for

    return classroom_token_for(user) is not None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moodle", "classroom"], required=True)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="owner email for synced courses")
    who.add_argument("--all-users", action="store_true",
                     help="every user with credentials for --source (use with --enqueue)")
    parser.add_argument("--course", default=None, help="source course id (default: all)")
    parser.add_argument("--enqueue", action="store_true",
                        help="queue a sync job for the worker instead of running inline")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.all_users:
            users = [u for u in session.exec(select(User)).all()
                     if has_credentials(args.source, u)]
            if not users:
                print(f"no users have connected {args.source}")
            failed = 0
            for u in users:
                if args.enqueue:
                    from app.jobs import enqueue_sync_once

                    job = enqueue_sync_once(session, args.source, u.email, args.course)
                    print(f"enqueued {job.id} for {u.email}" if job
                          else f"{u.email}: a sync is already queued")
                    continue
                try:  # one user's revoked token or outage mustn't stop the rest
                    _sync_inline(session, args.source, u, args.course)
                except Exception as e:
                    session.rollback()
                    failed += 1
                    print(f"{u.email}: sync failed: {type(e).__name__}: {e}",
                          file=sys.stderr)
            if failed:
                raise SystemExit(f"sync failed for {failed} of {len(users)} users")
            return

        from app.auth import find_user

        user = find_user(session, args.user)
        if user is None:
            raise SystemExit(f"no such user {args.user} — log in via the web UI first")
        if args.enqueue:
            from app.jobs import enqueue_sync_once

            job = enqueue_sync_once(session, args.source, user.email, args.course)
            if job is None:
                print(f"a {args.source} sync is already queued for {user.email}")
            else:
                print(f"enqueued {job.id} (run `python -m app.worker` to drain)")
            return

        try:
            _sync_inline(session, args.source, user, args.course)
        except NotConnectedError as e:
            raise SystemExit(str(e)) from None


def _sync_inline(session, source: str, user: User, course: str | None) -> None:
    from app.sync import failed_courses, sync_all, sync_course

    adapter = build_adapter(source, user)
    if course:
        stats = sync_course(session, adapter, course, user.id)
        print(user.email, course, stats.as_dict())
        return
    results = sync_all(session, adapter, user.id)
    for course_id, stats in results.items():
        print(user.email, course_id, stats.as_dict())
    failed = failed_courses(results)
    if results and len(failed) == len(results):
        raise RuntimeError(f"every course failed, e.g. {results[failed[0]].error}")


if __name__ == "__main__":
    main()
