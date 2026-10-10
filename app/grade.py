"""Grading + spaced-repetition scheduling (Phase 4).

- MCQ: instant index-match, no API call.
- Short-answer: one fast-LLM call, lenient on phrasing, scored against the
  item's grading_criteria key points -> {correct, partial_credit, feedback}.
- partial_credit (0.0-1.0) feeds SM-2 through the locked Phase-0 mapping
  (partial_credit_to_quality), then ReviewState advances via srs.next_interval_days.
- ReviewState rows are created lazily on first answer; items without one
  count as due immediately (equivalent to next_review_date = now at creation).
- Only due items can be answered: a replayed or concurrent submit is rejected
  (NotDue) instead of advancing the schedule twice.
- A user can skip an item (to the back of that day's queue) or suspend it
  (never scheduled again, with the reason kept): see ItemFlag.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, tzinfo

from sqlalchemy import and_, or_
from sqlalchemy.dialects import postgresql, sqlite
from sqlmodel import Session, func, select

from app.extract import strip_nul
from app.llm import LLMClient
from app.llm_schemas import GradeOut
from app.models import (
    Chunk,
    Course,
    ItemFlag,
    QuizItem,
    Resource,
    ReviewLog,
    ReviewState,
    Topic,
    User,
)
from app.srs import (
    PASS_CREDIT,
    initial_ease_factor,
    local_day_start,
    next_interval_days,
    next_review_date,
    partial_credit_to_quality,
    verdict,
)

MAX_ANSWER_CHARS = 4000
NEW_ITEMS_PER_DAY = 20
MAX_NOTE_CHARS = 500
# why a question was suspended: stored key -> the label on the form
SUSPEND_REASONS = {
    "wrong_answer": "The marked answer is wrong",
    "unclear": "The question is unclear or broken",
    "off_topic": "It isn't about the course material",
    "duplicate": "It repeats another question",
    "other": "Something else",
}

GRADE_SYSTEM = """You grade a student's short answer leniently on phrasing.
You are given the question, a reference answer, and the key points a correct
answer must hit (grading criteria). Award partial credit when some key points
are present. Ignore grammar/spelling unless it changes meaning.
Return JSON: {"correct": true|false, "partial_credit": 0.0-1.0, "feedback": "1-2 sentences, kind, specific"}"""


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo — assume UTC for naive datetimes."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def user_zone(session: Session, user_id) -> tzinfo:
    """The user's study-day zone (settings.timezone unless they picked one)."""
    from app.config import settings

    user = session.get(User, user_id)
    return settings.zone(user.timezone if user else None)


def grade_short_answer(llm: LLMClient, item: QuizItem, answer: str) -> dict:
    data = llm.complete_json(
        GRADE_SYSTEM,
        f"Question: {item.question}\nReference answer: {item.correct_answer}\n"
        f"Key points: {item.grading_criteria}\nStudent answer: {answer}",
        temperature=0.0,
    )
    graded = GradeOut.model_validate(data)
    return {
        "correct": graded.partial_credit >= PASS_CREDIT,
        "partial_credit": graded.partial_credit,
        "feedback": graded.feedback,
    }


class InvalidAnswer(ValueError):
    """The submitted answer can't be graded against this item. The message is
    shown to the student."""


class NotDue(Exception):
    """The item isn't due for this user: already answered (a replayed or double
    submit, which would advance the schedule twice) or new past the daily cap."""


def _mcq_index(value: str, n_options: int) -> int | None:
    try:
        idx = int(str(value).strip())
    except ValueError:
        return None
    return idx if 0 <= idx < n_options else None


def _stored_mcq_index(value, n_options: int) -> int | None:
    """The item's correct option. Older rows stored the model's raw value,
    so accept "2.0" and "True"/"False" as well as "2"."""
    text = str(value).strip()
    if text in ("True", "False"):
        return _mcq_index(str(int(text == "True")), n_options)
    try:
        number = float(text)
    except ValueError:
        return None
    return _mcq_index(str(int(number)), n_options) if number.is_integer() else None


def mcq_index(value, item: QuizItem) -> int | None:
    """A submitted answer as an index into the item's options, or None."""
    return _mcq_index(value, len(item.options or []))


def correct_mcq_index(item: QuizItem) -> int | None:
    """The item's correct option, or None when the stored key is unusable."""
    return _stored_mcq_index(item.correct_answer, len(item.options or []))


def grade_mcq(item: QuizItem, answer: str) -> float:
    chosen = mcq_index(answer, item)
    if chosen is None:
        raise InvalidAnswer("Pick one of the listed options.")
    return 1.0 if chosen == correct_mcq_index(item) else 0.0


def _is_due(state: ReviewState | None, now: datetime) -> bool:
    return state is None or _aware(state.next_review_date) <= now


def _state_query(user_id, item_id):
    return select(ReviewState).where(
        ReviewState.user_id == user_id, ReviewState.quiz_item_id == item_id
    )


def _ensure_state(session: Session, user_id, item: QuizItem, now: datetime) -> None:
    """Create the user's ReviewState for this item unless one exists. ON
    CONFLICT DO NOTHING, so two concurrent first answers can't both insert
    and 500 on uq_review_user_item."""
    dialect = {"postgresql": postgresql, "sqlite": sqlite}[session.get_bind().dialect.name]
    session.exec(
        dialect.insert(ReviewState)
        .values(
            id=uuid.uuid4(), user_id=user_id, quiz_item_id=item.id,
            ease_factor=initial_ease_factor(item.difficulty), interval_days=0,
            next_review_date=now, repetitions=0, lapses=0,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "quiz_item_id"])
    )


def _lock_user(session: Session, user_id) -> None:
    """Row-lock the user until commit/rollback, serializing their new-item
    submits so each sees the others' spent daily slots."""
    session.exec(select(User.id).where(User.id == user_id).with_for_update()).one()


def submit_answer(
    session: Session, user_id, quiz_item_id, answer: str, llm: LLMClient | None = None
) -> dict:
    """Grade an answer and advance the SM-2 schedule. Returns the outcome.

    Raises NotDue if the item isn't due, checked before grading (no LLM call
    for a replay) and again under a row lock before the schedule moves. A new
    item is also held to the daily cap, checked under a user-row lock.
    """
    item = session.get(QuizItem, quiz_item_id)
    if item is None:
        raise ValueError(f"quiz item {quiz_item_id} not found")
    answer = strip_nul(answer)  # stored in last_answer: Postgres rejects NUL
    if len(answer) > MAX_ANSWER_CHARS:
        raise InvalidAnswer(f"Answers are limited to {MAX_ANSWER_CHARS:,} characters.")
    now = datetime.now(timezone.utc)
    state = session.exec(_state_query(user_id, item.id)).first()
    if state is None:
        # A first answer spends a daily slot, but first_answered_at only lands
        # at commit. Hold the user lock through commit (grading included) so
        # concurrent new-item submits can't overspend the last slot or both
        # pay for an LLM call; re-read what a submit we waited on committed.
        _lock_user(session, user_id)
        state = session.exec(_state_query(user_id, item.id)).first()
        if state is None and _new_allowance(session, user_id, now) <= 0:
            raise NotDue(quiz_item_id)  # a new item past today's cap isn't in the queue
    # Only a first answer spends a slot: a row predating first_answered_at
    # (NULL after migration 0008) is a review, not a new item.
    is_new_item = state is None
    if not _is_due(state, now):
        raise NotDue(quiz_item_id)

    if item.question_type == "mcq":
        partial = grade_mcq(item, answer)
        feedback = item.explanation or ""
    else:
        if llm is None:
            raise ValueError("short-answer grading needs an LLM client")
        graded = grade_short_answer(llm, item, answer)
        partial, feedback = graded["partial_credit"], graded["feedback"]

    quality = partial_credit_to_quality(partial)
    now = datetime.now(timezone.utc)
    _ensure_state(session, user_id, item, now)
    # Serialize concurrent submits: the loser waits here, then sees the
    # winner's future next_review_date and is rejected.
    state = session.exec(
        _state_query(user_id, item.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if not _is_due(state, now):
        session.rollback()
        raise NotDue(quiz_item_id)
    interval, reps, ease = next_interval_days(
        quality, state.repetitions, state.ease_factor, state.interval_days
    )
    state.interval_days = interval
    state.repetitions = reps
    state.ease_factor = ease
    state.last_result = verdict(partial)
    state.last_feedback = feedback
    state.last_answer = answer
    if quality < 3:
        state.lapses += 1
    state.next_review_date = next_review_date(interval, now, user_zone(session, user_id))
    state.answered_at = now
    if is_new_item and state.first_answered_at is None:
        state.first_answered_at = now
    session.add(state)
    session.add(ReviewLog(user_id=user_id, quiz_item_id=item.id, verdict=state.last_result,
                          partial_credit=partial, answered_at=now))
    session.commit()
    session.refresh(state)
    return {
        "correct": partial >= PASS_CREDIT,
        "partial_credit": partial,
        "quality": quality,
        "feedback": feedback,
        "verdict": state.last_result,
        "interval_days": state.interval_days,
        "repetitions": state.repetitions,
        "next_review_date": _aware(state.next_review_date),
    }


def scoped_items(user_id, *entities):
    """QuizItems in the user's courses. Unclaimed pre-auth rows belong to
    nobody until their owner signs in or syncs (see auth.sign_in, sync).
    Selects `entities` instead when given, for rows joined on by the caller."""
    return (
        select(*(entities or (QuizItem,)))
        .select_from(QuizItem)
        .join(Chunk, Chunk.id == QuizItem.chunk_id)
        .join(Resource, Resource.id == Chunk.resource_id)
        .join(Topic, Topic.id == Resource.topic_id)
        .join(Course, Course.id == Topic.course_id)
        .where(Course.user_id == user_id)
    )


def user_owns_item(session: Session, user_id, item_id) -> bool:
    return session.exec(
        scoped_items(user_id).where(QuizItem.id == item_id).with_only_columns(QuizItem.id)
    ).first() is not None


def active_items(user_id, *entities):
    """Scoped items up for review: those outside archived courses that the
    user hasn't suspended. Joins the user's ItemFlag, when there is one."""
    return (
        scoped_items(user_id, *entities)
        .outerjoin(ItemFlag, and_(ItemFlag.quiz_item_id == QuizItem.id,
                                  ItemFlag.user_id == user_id))
        .where(Course.archived == False, ItemFlag.suspended_at.is_(None))  # noqa: E712
    )


def _new(user_id):
    """Active items this user has never answered."""
    return active_items(user_id).outerjoin(
        ReviewState,
        and_(ReviewState.quiz_item_id == QuizItem.id, ReviewState.user_id == user_id),
    ).where(ReviewState.id.is_(None))


def _overdue(user_id, now: datetime):
    """Active items whose ReviewState has come due."""
    return active_items(user_id).join(
        ReviewState,
        and_(ReviewState.quiz_item_id == QuizItem.id, ReviewState.user_id == user_id),
    ).where(ReviewState.next_review_date <= now)


def _new_allowance(session: Session, user_id, now: datetime) -> int:
    """New items still allowed today (the user's local day) under NEW_ITEMS_PER_DAY."""
    day_start = local_day_start(now, user_zone(session, user_id))
    started = session.exec(
        select(func.count(ReviewState.id)).where(
            ReviewState.user_id == user_id, ReviewState.first_answered_at >= day_start
        )
    ).one()
    return max(0, NEW_ITEMS_PER_DAY - started)


def due_items(session: Session, user_id, limit: int = 20) -> list[QuizItem]:
    """Review queue: most-overdue reviews first, then new items up to the
    daily cap, so a backlog of new items can't starve reviews. Items skipped
    today come last, in the order they were skipped."""
    now = datetime.now(timezone.utc)
    day_start = local_day_start(now, user_zone(session, user_id))
    unskipped = or_(ItemFlag.skipped_at.is_(None), ItemFlag.skipped_at < day_start)
    items = list(session.exec(
        _overdue(user_id, now)
        .where(unskipped)
        .order_by(ReviewState.next_review_date, Topic.order, Chunk.order,
                  QuizItem.generation_key)
        .limit(limit)
    ).all())
    allowance = _new_allowance(session, user_id, now)
    room = min(limit - len(items), allowance)
    if room > 0:
        fresh = session.exec(
            _new(user_id)
            .where(unskipped)
            .order_by(Topic.order, Chunk.order, QuizItem.generation_key)
            .limit(room)
        ).all()
        items += fresh
        allowance -= len(fresh)
    if len(items) < limit:
        skipped = session.exec(
            active_items(user_id, QuizItem, ReviewState.id)
            .outerjoin(ReviewState, and_(ReviewState.quiz_item_id == QuizItem.id,
                                         ReviewState.user_id == user_id))
            .where(ItemFlag.skipped_at >= day_start,
                   or_(ReviewState.id.is_(None), ReviewState.next_review_date <= now))
            .order_by(ItemFlag.skipped_at, QuizItem.generation_key)
        ).all()
        for item, state_id in skipped:
            if len(items) >= limit:
                break
            if state_id is None:  # a new item: still held to the daily cap
                if allowance <= 0:
                    continue
                allowance -= 1
            items.append(item)
    return items


def due_count(session: Session, user_id) -> int:
    now = datetime.now(timezone.utc)
    overdue = session.exec(
        _overdue(user_id, now).with_only_columns(func.count(QuizItem.id))
    ).one()
    new = session.exec(_new(user_id).with_only_columns(func.count(QuizItem.id))).one()
    return overdue + min(new, _new_allowance(session, user_id, now))


def _flag(session: Session, user_id, item_id, **values) -> None:
    """Upsert the user's ItemFlag for an item, so a double click can't 500
    on the primary key."""
    dialect = {"postgresql": postgresql, "sqlite": sqlite}[session.get_bind().dialect.name]
    session.exec(
        dialect.insert(ItemFlag)
        .values(user_id=user_id, quiz_item_id=item_id, **values)
        .on_conflict_do_update(index_elements=["user_id", "quiz_item_id"], set_=values)
    )
    session.commit()


def skip_item(session: Session, user_id, item_id) -> None:
    """Send an item to the back of today's queue. Its schedule is untouched."""
    _flag(session, user_id, item_id, skipped_at=datetime.now(timezone.utc))


def suspend_item(session: Session, user_id, item_id, reason: str, note: str = "") -> None:
    """Stop scheduling an item for this user, recording why. An unknown
    reason is filed under "other" rather than refused."""
    if reason not in SUSPEND_REASONS:
        reason = "other"
    note = strip_nul(note).strip()[:MAX_NOTE_CHARS]  # Postgres rejects NUL
    _flag(session, user_id, item_id, suspended_at=datetime.now(timezone.utc),
          reason=reason, note=note or None)


def restore_item(session: Session, user_id, item_id) -> None:
    """Put a suspended item back on its schedule."""
    _flag(session, user_id, item_id, suspended_at=None, reason=None, note=None)


def suspended_items(session: Session, user_id) -> list[tuple[QuizItem, ItemFlag]]:
    """The user's suspended items with their flags, latest first."""
    return list(session.exec(
        scoped_items(user_id, QuizItem, ItemFlag)
        .join(ItemFlag, and_(ItemFlag.quiz_item_id == QuizItem.id,
                             ItemFlag.user_id == user_id))
        .where(ItemFlag.suspended_at.is_not(None))
        .order_by(ItemFlag.suspended_at.desc(), QuizItem.generation_key)
    ).all())
