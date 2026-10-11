"""Deleting an account: every row kept for one user, and nothing else.

Courses, topics and resources are per-user rows; app.share copies results
between users and never points one user's rows at another's. So removing a
user's tree can't take material from anyone still enrolled: their own copy
of the same file, with its chunks and quiz items, stays.
"""

from __future__ import annotations

from contextlib import ExitStack

from sqlalchemy import delete, or_
from sqlmodel import Session, select

from app.auth import refresh_token_for, revoke_google_token
from app.jobs import SOURCES, sync_lock
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


class SyncRunning(RuntimeError):
    """A sync of the user's courses is writing their rows right now."""


def delete_account(session: Session, user: User) -> bool:
    """Delete the user with everything kept for them: tokens, courses and
    what was built from them, review history, notification events and queued
    jobs; then revoke their Google grant. Their sessions end with the row
    (main.current_user). Raises SyncRunning, with nothing deleted, while a
    sync of theirs runs: it would write courses for a user who is gone.

    The grant is revoked only once the rows are gone: a deletion that fails
    leaves the account as it was, still connected.

    Returns whether Google confirmed the revocation (see
    revoke_google_token); the account is deleted either way."""
    user_id, email = user.id, user.email
    token = refresh_token_for(user)  # the row it is read from is about to go
    bind = session.get_bind()
    with ExitStack() as locks:
        for source in SOURCES:
            if not locks.enter_context(sync_lock(source, email, bind)):
                raise SyncRunning(email)
        _delete_rows(session, user_id, email)
    return revoke_google_token(token)


def _delete_rows(session: Session, user_id, email: str) -> None:
    course_ids = select(Course.id).where(Course.user_id == user_id)
    topic_ids = select(Topic.id).where(Topic.course_id.in_(course_ids))
    resource_ids = select(Resource.id).where(Resource.topic_id.in_(topic_ids))
    chunk_ids = select(Chunk.id).where(Chunk.resource_id.in_(resource_ids))
    item_ids = select(QuizItem.id).where(QuizItem.chunk_id.in_(chunk_ids))
    # A pipeline run isn't locked out (its lock is per source, held for the
    # whole cron run) and skips rows that vanish under it. What it may still
    # insert for this user, e.g. a new-material event, needs the user row:
    # holding it makes that wait, then fail, instead of failing our delete.
    session.exec(select(User.id).where(User.id == user_id).with_for_update()).first()
    # children first, explicitly: SQLite doesn't enforce ON DELETE CASCADE
    for stmt in (
        delete(ReviewState).where(or_(ReviewState.user_id == user_id,
                                      ReviewState.quiz_item_id.in_(item_ids))),
        delete(ItemFlag).where(or_(ItemFlag.user_id == user_id,
                                   ItemFlag.quiz_item_id.in_(item_ids))),
        delete(ReviewLog).where(ReviewLog.user_id == user_id),
        delete(NotificationEvent).where(NotificationEvent.user_id == user_id),
        delete(QuizItem).where(QuizItem.chunk_id.in_(chunk_ids)),
        delete(QuizAttempt).where(QuizAttempt.chunk_id.in_(chunk_ids)),
        delete(QuizFailure).where(QuizFailure.chunk_id.in_(chunk_ids)),
        delete(Chunk).where(Chunk.resource_id.in_(resource_ids)),
        delete(Assignment).where(Assignment.course_id.in_(course_ids)),
        delete(Resource).where(Resource.topic_id.in_(topic_ids)),
        delete(Topic).where(Topic.course_id.in_(course_ids)),
        delete(Course).where(Course.user_id == user_id),
        # queued and finished jobs carry the address; a running one finds
        # its row gone and drops its result (jobs._finish)
        delete(Job).where(Job.payload["user_email"].as_string() == email),
        delete(User).where(User.id == user_id),
    ):
        session.execute(stmt.execution_options(synchronize_session=False))
    session.commit()
    session.expunge_all()  # nothing loaded for the user is valid any more
