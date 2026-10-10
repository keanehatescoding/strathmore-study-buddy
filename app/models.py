"""SQLModel entities. Mirrors the project-plan schema (Phase 0)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(unique=True, index=True)
    google_refresh_token: Optional[str] = Field(default=None)
    moodle_token: Optional[str] = Field(default=None)
    # bumped to revoke every session: sessions carry the value they began with
    session_version: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    # IANA zone whose midnight starts this user's study day; NULL = settings.timezone
    timezone: Optional[str] = Field(default=None)
    # email notifications at all; off from settings or an unsubscribe link
    notify_email: bool = Field(default=True, sa_column_kwargs={"server_default": true()})
    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class Course(SQLModel, table=True):
    __tablename__ = "courses"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "source_id", name="uq_courses_user_source"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(default=None, foreign_key="users.id", index=True)
    source: str = Field(index=True)  # "moodle" | "classroom"
    source_id: str = Field(index=True)
    name: str
    code: Optional[str] = Field(default=None)
    # set by its owner: off Courses, the review queue and the pipeline;
    # sync still updates it and its history stays
    archived: bool = Field(default=False, sa_column_kwargs={"server_default": false()})


class Topic(SQLModel, table=True):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("course_id", "source_id", name="uq_topics_course_source"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    course_id: uuid.UUID = Field(foreign_key="courses.id", index=True)
    source_id: str
    title: str
    order: int = Field(default=0)


class Resource(SQLModel, table=True):
    __tablename__ = "resources"
    __table_args__ = (UniqueConstraint("topic_id", "source_id", name="uq_resources_topic_source"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    topic_id: uuid.UUID = Field(foreign_key="topics.id", index=True)
    source: str  # "moodle" | "classroom"
    source_id: str = Field(index=True)
    type: str  # "file" | "link" | "page_text" | "video"
    title: str
    raw_url: Optional[str] = Field(default=None)
    extracted_text: Optional[str] = Field(default=None, sa_column=Column(Text))
    content_hash: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="pending", index=True)  # pending|extracted|failed|skipped
    error: Optional[str] = Field(default=None)
    mime_type: Optional[str] = Field(default=None)
    # consecutive failed pipeline tries (download/chunking) and when the next
    # may run, so a dead download or a failing LLM call isn't redone every run
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    retry_after: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class Assignment(SQLModel, table=True):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("course_id", "source_id", name="uq_assignments_course_source"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    course_id: uuid.UUID = Field(foreign_key="courses.id", index=True)
    topic_id: Optional[uuid.UUID] = Field(default=None, foreign_key="topics.id", index=True)
    source: str = Field(default="moodle")
    source_id: str = Field(index=True)
    title: str
    due_date: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    description: Optional[str] = Field(default=None, sa_column=Column(Text))


class Chunk(SQLModel, table=True):
    __tablename__ = "chunks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    resource_id: uuid.UUID = Field(foreign_key="resources.id", index=True, ondelete="CASCADE")
    title: str
    content: str = Field(sa_column=Column(Text, nullable=False))
    order: int = Field(default=0)
    start_char: Optional[int] = Field(default=None)
    end_char: Optional[int] = Field(default=None)


class QuizAttempt(SQLModel, table=True):
    """A quiz generation that ran for (chunk, attempt), even one that yielded
    no items, so a chunk with nothing quizzable isn't re-billed every run."""

    __tablename__ = "quiz_attempts"

    chunk_id: uuid.UUID = Field(foreign_key="chunks.id", primary_key=True,
                                ondelete="CASCADE")
    attempt: int = Field(primary_key=True)


class QuizFailure(SQLModel, table=True):
    """Consecutive failed generations for (chunk, attempt) and when the next
    may run, so a chunk the LLM can't quiz isn't re-billed on every run.
    Deleted on success; at MAX_QUIZ_FAILURES the chunk is given up on."""

    __tablename__ = "quiz_failures"

    chunk_id: uuid.UUID = Field(foreign_key="chunks.id", primary_key=True,
                                ondelete="CASCADE")
    attempt: int = Field(primary_key=True)
    failures: int = Field(default=0)
    retry_after: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    error: Optional[str] = Field(default=None)


class QuizItem(SQLModel, table=True):
    __tablename__ = "quiz_items"
    __table_args__ = (UniqueConstraint("generation_key", name="uq_quiz_items_gen_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    chunk_id: uuid.UUID = Field(foreign_key="chunks.id", index=True, ondelete="CASCADE")
    question: str = Field(sa_column=Column(Text, nullable=False))
    question_type: str  # "mcq" | "short_answer"
    options: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    correct_answer: str = Field(sa_column=Column(Text, nullable=False))  # index-as-str for mcq
    grading_criteria: Optional[str] = Field(default=None, sa_column=Column(Text))
    explanation: Optional[str] = Field(default=None, sa_column=Column(Text))
    difficulty: str = Field(default="recall")  # recall|application|synthesis
    generation_key: str = Field(index=True)  # chunk_id + attempt, idempotency


class ReviewState(SQLModel, table=True):
    __tablename__ = "review_states"
    __table_args__ = (UniqueConstraint("user_id", "quiz_item_id", name="uq_review_user_item"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    quiz_item_id: uuid.UUID = Field(
        foreign_key="quiz_items.id", index=True, ondelete="CASCADE"
    )
    ease_factor: float = Field(default=2.5)
    interval_days: int = Field(default=0)
    next_review_date: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    last_result: Optional[str] = Field(default=None)
    repetitions: int = Field(default=0)
    lapses: int = Field(default=0)
    answered_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # when the item left the "new" pool; feeds the daily new-item cap
    first_answered_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # the grader's feedback on the last answer, shown on the result page
    last_feedback: Optional[str] = Field(default=None, sa_column=Column(Text))
    # the answer as submitted (option index for an MCQ), shown beside the key
    last_answer: Optional[str] = Field(default=None, sa_column=Column(Text))


class ItemFlag(SQLModel, table=True):
    """A user's say on one question, apart from its schedule: skipped (to the
    back of that day's queue) or suspended (never scheduled) with the reason."""

    __tablename__ = "item_flags"

    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)
    quiz_item_id: uuid.UUID = Field(
        foreign_key="quiz_items.id", primary_key=True, index=True, ondelete="CASCADE"
    )
    skipped_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    suspended_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # why it was suspended (a grade.SUSPEND_REASONS key) and the user's own
    # words: kept for regenerating the question
    reason: Optional[str] = Field(default=None)
    note: Optional[str] = Field(default=None, sa_column=Column(Text))


class ReviewLog(SQLModel, table=True):
    """One row per graded answer. ReviewState keeps only the latest answer,
    so history (streaks, accuracy) is read from here."""

    __tablename__ = "review_logs"
    __table_args__ = (Index("ix_review_logs_user_answered", "user_id", "answered_at"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id")
    # history outlives a regenerated item
    quiz_item_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="quiz_items.id", ondelete="SET NULL"
    )
    verdict: str  # srs.verdict(): correct | partial | incorrect
    partial_credit: Optional[float] = Field(default=None)  # NULL on backfilled rows
    answered_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class NotificationEvent(SQLModel, table=True):
    __tablename__ = "notification_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    type: str = Field(index=True)  # "new_material" | "review_due"
    payload: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    sent: bool = Field(default=False, index=True)
    sent_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # Multi-email batch this event was last sent in; kept so an ambiguous
    # failure is retried as the same batch. "b2-..." only groups (the key is
    # derived from the payload); legacy "batch-..." was the Idempotency-Key.
    batch_key: Optional[str] = Field(default=None, index=True)
    # set when the event can never be delivered (no recipient, opted out, ...);
    # such events leave the queue instead of being retried every pass
    failed_reason: Optional[str] = Field(default=None)
    # failed delivery tries; at MAX_SEND_ATTEMPTS the event gives up
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})


ACTIVE_NOTIFY_WHERE = "type = 'send_notifications' AND status IN ('pending', 'running')"
ACTIVE_USER_JOB_WHERE = "type IN ('sync', 'pipeline') AND status IN ('pending', 'running')"


class Job(SQLModel, table=True):
    """Postgres-backed job queue (no Redis/Celery at this scale)."""

    __tablename__ = "jobs"
    __table_args__ = (
        # At most one queued/running notify job, so racing workers can't both
        # enqueue one and double-send (see app.worker).
        Index(
            "uq_jobs_active_notify", "type", unique=True,
            postgresql_where=text(ACTIVE_NOTIFY_WHERE),
            sqlite_where=text(ACTIVE_NOTIFY_WHERE),
        ),
        # At most one queued/running sync (or pipeline) job per source, user
        # and course ('' for all courses), so repeat submissions don't pile
        # up (app.jobs._enqueue_once); the sync handler runs them one at a
        # time. `->>` works on Postgres json and SQLite >= 3.38 alike.
        Index(
            "uq_jobs_active_user_job", "type",
            text("(payload ->> 'source')"), text("(payload ->> 'user_email')"),
            text("(COALESCE(payload ->> 'course_id', ''))"),
            unique=True,
            postgresql_where=text(ACTIVE_USER_JOB_WHERE),
            sqlite_where=text(ACTIVE_USER_JOB_WHERE),
        ),
        # Claiming / the notify busy check (status + available_at), and the
        # reaper / pruning (status + updated_at). Both lead with status, so
        # they also cover plain status lookups.
        Index("ix_jobs_status_available_at", "status", "available_at"),
        Index("ix_jobs_status_updated_at", "status", "updated_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    type: str = Field(index=True)  # "sync" | "send_notifications" (see app.jobs.HANDLERS)
    payload: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: str = Field(default="pending")  # pending|running|completed|failed
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)
    error: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    available_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
