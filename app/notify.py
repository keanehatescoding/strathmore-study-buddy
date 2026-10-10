"""Notifications (Phase 6): batched new-material + threshold review-due, via email.

- Generation (what to send) is decoupled from delivery: NotificationEvent
  rows with sent=False ARE the queue; the worker sends them.
- new_material: one event per course ("8 new quiz items from CS 301"), never
  per-item. A generation run adds its count to the course's queued, untried
  event instead of queueing another, so a pipeline job sliced into many runs
  sends one email. Unsent after NEW_MATERIAL_MAX_AGE, it's old news and drops.
- review_due: created only when due count >= threshold (avoids fatigue),
  never while an unsent one exists, and not within REVIEW_DUE_COOLDOWN of the
  last one's delivery (the worker runs hourly; the due count stays high until
  the user reviews), and not for users who haven't answered (or signed up)
  within REVIEW_DUE_ACTIVE_WINDOW: a student who synced once and walked away
  isn't nagged forever.
- Opt-out: users.notify_email (settings page, or the signed one-click
  unsubscribe link every email carries, with List-Unsubscribe headers).
  Events that can never be delivered (opted out, gone inactive, no
  recipient, unknown type, expired) get failed_reason and leave the queue,
  as do events that failed MAX_SEND_ATTEMPTS deliveries (bad address, bad
  key): no endless retries, no burst of days-old mail after an outage.
- Delivery: Resend batch REST API (stdlib only, free tier), up to 100 emails
  per request, backing off on 429. No key -> events stay queued; nothing is
  fake-marked sent.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from itsdangerous import BadSignature, URLSafeSerializer
from sqlmodel import Session, func, select

from app.grade import _aware, due_count
from app.models import Course, NotificationEvent, Resource, ReviewLog, Topic, User

REVIEW_DUE_THRESHOLD = 3
REVIEW_DUE_COOLDOWN = timedelta(hours=23)  # daily, without drifting an hour a day
REVIEW_DUE_ACTIVE_WINDOW = timedelta(days=14)
UNSUBSCRIBE_SALT = "unsubscribe"
RESEND_BATCH_URL = "https://api.resend.com/emails/batch"
BATCH_SIZE = 100  # Resend's per-request batch limit
MAX_RETRIES = 4
MAX_RETRY_WAIT = 60.0  # longer waits give up; the next worker pass retries
MAX_SEND_ATTEMPTS = 24  # failed passes before an event gives up (~a day, hourly)
NEW_MATERIAL_MAX_AGE = timedelta(days=2)


class EmailError(RuntimeError):
    """`reason` is a short fixed-vocabulary token, safe to persist in job results
    (no provider message text, which can echo addresses)."""

    def __init__(self, message: str, reason: str = "error"):
        super().__init__(message)
        self.reason = reason


class RateLimitedError(EmailError):
    """Resend kept answering 429; stop and leave events queued."""

    def __init__(self, message: str):
        super().__init__(message, "rate_limited")


def _retry_after(err: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait: Retry-After as delta-seconds or HTTP-date, else 2**attempt."""
    value = (err.headers.get("Retry-After") or "").strip() if err.headers else ""
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return float(2 ** attempt)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _http_reason(err: urllib.error.HTTPError) -> str:
    """`http_<code>[:<resend error name>]`, e.g. http_422:validation_error."""
    name = None
    try:
        body = json.loads(err.read(2048) or b"{}")
        name = body.get("name") if isinstance(body, dict) else None
    except (OSError, ValueError):  # unreadable or non-JSON body: status only
        pass
    if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,40}", name):
        return f"http_{err.code}:{name}"
    return f"http_{err.code}"


def send_batch(api_key: str, emails: list[dict], idempotency_key: str,
               sleep=time.sleep) -> None:
    """POST up to BATCH_SIZE emails in one request; retries 429 with backoff.

    Resend dedupes on Idempotency-Key for 24h, so resending the same batch
    with the same key after a lost response doesn't deliver twice.
    """
    if not api_key:
        raise EmailError("RESEND_API_KEY is empty — set it in .env", "no_api_key")
    req = urllib.request.Request(
        RESEND_BATCH_URL,
        data=json.dumps(emails).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": idempotency_key,
            # Resend's edge filter 403s the default urllib agent
            "User-Agent": "study-buddy/0.1",
        },
        method="POST",
    )
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 201, 202):
                    raise EmailError(f"resend returned {resp.status}",
                                     f"http_{resp.status}")
                return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                reason = _http_reason(e)
                raise EmailError(f"resend returned {reason}", reason) from e
            wait = _retry_after(e, attempt)
            if attempt == MAX_RETRIES or wait > MAX_RETRY_WAIT:
                raise RateLimitedError(f"resend rate limit (retry after {wait:.0f}s)") from e
            sleep(wait)
        except EmailError:
            raise
        except Exception as e:
            raise EmailError(f"send failed: {e}", f"network:{type(e).__name__}") from e


def enqueue_new_material(session: Session, course_id, new_items: int) -> NotificationEvent | None:
    """One batched event per course, owned by the course owner.

    Adds to the course's queued event when it has never been tried (an
    attempted one may already have been delivered with its old count), so
    each slice of a long pipeline job doesn't queue another email.

    Returns None when nothing is new or the course has no owner: there is
    nobody to tell, and guessing a recipient would misattribute the course.
    """
    if new_items <= 0:
        return None
    course = session.get(Course, course_id)
    if course is None or course.user_id is None:
        return None
    owner = session.get(User, course.user_id)
    if owner is None or not owner.notify_email:
        return None
    queued = session.exec(
        select(NotificationEvent).where(
            NotificationEvent.user_id == course.user_id,
            NotificationEvent.type == "new_material",
            NotificationEvent.sent == False,  # noqa: E712
            NotificationEvent.failed_reason == None,  # noqa: E711
            NotificationEvent.batch_key == None,  # noqa: E711
            NotificationEvent.attempts == 0,
        ).order_by(NotificationEvent.created_at).with_for_update()
    ).all()
    event = next((e for e in queued if e.payload.get("course_id") == str(course.id)), None)
    if event is None:
        event = NotificationEvent(user_id=course.user_id, type="new_material")
    else:
        new_items += event.payload.get("new_items", 0)
        event.created_at = datetime.now(timezone.utc)  # fresh news: restart the expiry
    event.payload = {"course": course.name, "code": course.code,
                     "course_id": str(course.id), "new_items": new_items}
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def check_review_due(
    session: Session, user_id, threshold: int = REVIEW_DUE_THRESHOLD
) -> NotificationEvent | None:
    """Create a review_due event if the user wants email and is still
    studying, due >= threshold, none is still queued, and the last one was
    delivered more than REVIEW_DUE_COOLDOWN ago."""
    user = session.get(User, user_id)
    if user is None or not user.notify_email or not _recently_active(session, user):
        return None
    due = due_count(session, user_id)
    if due < threshold:
        return None
    last = session.exec(
        select(NotificationEvent)
        .where(NotificationEvent.user_id == user_id, NotificationEvent.type == "review_due",
               NotificationEvent.failed_reason == None)  # noqa: E711
        .order_by(NotificationEvent.created_at.desc())
    ).first()
    if last is not None and (
        not last.sent
        # from delivery, not creation: a long-queued event must not open the
        # window at once; rows sent before sent_at existed fall back
        or _aware(last.sent_at or last.created_at)
        > datetime.now(timezone.utc) - REVIEW_DUE_COOLDOWN
    ):
        return None
    event = NotificationEvent(
        user_id=user_id, type="review_due", payload={"due_count": due}
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def _recently_active(session: Session, user: User) -> bool:
    """Answered something, or signed up, within REVIEW_DUE_ACTIVE_WINDOW."""
    last_answer = session.exec(
        select(func.max(ReviewLog.answered_at)).where(ReviewLog.user_id == user.id)
    ).one()
    since = datetime.now(timezone.utc) - REVIEW_DUE_ACTIVE_WINDOW
    return any(_aware(t) > since for t in (last_answer, user.created_at) if t is not None)


def _serializer(secret_key: str | None) -> URLSafeSerializer:
    if secret_key is None:
        from app.config import settings

        secret_key = settings.secret_key
    return URLSafeSerializer(secret_key, salt=UNSUBSCRIBE_SALT)


def unsubscribe_token(user_id, secret_key: str | None = None) -> str:
    """Signed, non-expiring token naming the user; it only ever turns email off."""
    return _serializer(secret_key).dumps(str(user_id))


def unsubscribe_user(session: Session, token: str,
                     secret_key: str | None = None) -> User | None:
    """The user a valid token names, else None."""
    try:
        user_id = uuid.UUID(_serializer(secret_key).loads(token))
    except (BadSignature, ValueError, TypeError):
        return None
    return session.get(User, user_id)


def unsubscribe_url(event: NotificationEvent, base_url: str,
                    secret_key: str | None = None) -> str:
    return f"{base_url.rstrip('/')}/unsubscribe/{unsubscribe_token(event.user_id, secret_key)}"


def _items(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def render(event: NotificationEvent, base_url: str | None = None,
           secret_key: str | None = None) -> tuple[str, str]:
    if base_url is None:
        from app.config import settings

        base_url = settings.app_base_url
    review_url = f"{base_url.rstrip('/')}/review"
    footer = ("\n\nTo stop these emails, unsubscribe: "
              f"{unsubscribe_url(event, base_url, secret_key)}")
    if event.type == "new_material":
        p = event.payload
        return (
            f"New study material: {p.get('code') or p.get('course')}",
            f"{_items(p['new_items'], 'new quiz item')} from {p.get('course')}.\n"
            f"Review them: {review_url}{footer}",
        )
    if event.type == "review_due":
        n = event.payload.get("due_count", 0)
        return (
            f"{_items(n, 'review')} due",
            f"You have {_items(n, 'quiz item')} due for review.\n"
            f"Catch up: {review_url}{footer}",
        )
    raise EmailError(f"unknown event type {event.type!r}", "unknown_event_type")


def recipient_for(session: Session, event: NotificationEvent, fallback: str = "") -> str:
    """Per-user recipient: the event owner's email, else the fallback."""
    if event.user_id is not None:
        user = session.get(User, event.user_id)
        if user is not None and user.email:
            return user.email
    return fallback


def opt_out(session: Session, user: User) -> None:
    """Turn email off and fail the user's queued events, so turning it back
    on before the next pass doesn't send what was queued while it was off."""
    user.notify_email = False
    session.add(user)
    for event in session.exec(
        select(NotificationEvent).where(
            NotificationEvent.user_id == user.id,
            NotificationEvent.sent == False,  # noqa: E712
            NotificationEvent.failed_reason == None,  # noqa: E711
        )
    ):
        event.failed_reason = "opted_out"
        session.add(event)
    session.commit()


def _undeliverable(session: Session, event: NotificationEvent) -> str | None:
    """Why the owner shouldn't get this event any more, else None."""
    if event.type == "new_material" and (
        _aware(event.created_at) < datetime.now(timezone.utc) - NEW_MATERIAL_MAX_AGE
    ):
        return "expired"
    user = session.get(User, event.user_id) if event.user_id is not None else None
    if user is None:
        return None  # recipient_for decides (fallback address)
    if not user.notify_email:
        return "opted_out"
    # a reminder queued long enough (outage, no API key) for its user to
    # go inactive is as stale as one check_review_due would now skip
    if event.type == "review_due" and not _recently_active(session, user):
        return "inactive"
    return None


# Reasons that will never change on retry: the event is marked failed, not requeued.
PERMANENT_FAILURES = {"no_recipient", "unknown_event_type", "opted_out", "inactive",
                      "expired"}
GAVE_UP = "gave_up"  # failed_reason after MAX_SEND_ATTEMPTS failed deliveries


# Resend rejected the batch before sending anything: safe to split and resend.
BATCH_REJECTED = "http_422:validation_error"
KEY_CONFLICT = "http_409:invalid_idempotent_request"  # same key, different body
LEGACY_BATCH_PREFIX = "batch-"  # batch_key that was itself the Idempotency-Key
BATCH_PREFIX = "b2-"  # batch_key that only groups; the key is _idempotency_key


def send_pending(
    session: Session, api_key: str, from_addr: str, fallback_to: str = "",
    base_url: str | None = None, sleep=time.sleep,
) -> dict:
    """Send all unsent events, each to its owner's email, BATCH_SIZE per request.

    One commit per delivered batch. Failures stay queued; a rate limit that
    outlasts the retries stops the run so the rest wait for the next pass.
    `errors` counts failures by EmailError.reason for the job result.
    Events that can never go out (PERMANENT_FAILURES) get failed_reason and
    leave the queue; any other failure counts an attempt, and the
    MAX_SEND_ATTEMPTS-th leaves it as GAVE_UP. A rate limit isn't the
    event's fault and costs no attempt.

    Duplicate safety: a multi-email batch gets a batch_key, committed before
    the request, so a failure that might have been delivered (network, 5xx,
    ...) leaves the batch as-is and later passes resend the same events. The
    Idempotency-Key hashes the event ids and request body (_idempotency_key):
    an unchanged resend reuses it, while a changed body (recipient dropped,
    new base URL) gets a fresh key instead of a 409 for 24h. Only a confirmed
    validation rejection splits a batch into single sends. Resend keeps keys
    for 24h; a batch still unconfirmed after that may be delivered twice.

    Batches persisted before payload-derived keys ("batch-..." batch_key) were
    sent with the batch_key itself, so they keep it until delivered, or until
    Resend confirms their body changed (409), when they move to BATCH_PREFIX.
    """
    sent = 0
    errors: Counter[str] = Counter()  # reason token -> failed deliveries
    events = session.exec(
        select(NotificationEvent)
        .where(NotificationEvent.sent == False,  # noqa: E712
               NotificationEvent.failed_reason == None)  # noqa: E711
        .order_by(NotificationEvent.created_at)
    ).all()
    if not api_key:  # nothing can be delivered; don't fake-mark or split batches
        errors["no_api_key"] = len(events)
        return _result(sent, errors)
    keyed: dict[str, list] = {}  # batch_key -> earlier batch, resent unchanged
    fresh: list = []
    if base_url is None:
        from app.config import settings

        base_url = settings.app_base_url
    for event in events:
        to_addr = recipient_for(session, event, fallback_to)
        try:
            if reason := _undeliverable(session, event):
                raise EmailError(f"not sending: {reason}", reason)
            if not to_addr:
                raise EmailError("no recipient", "no_recipient")
            subject, body = render(event, base_url)
        except EmailError as e:
            errors[e.reason] += 1
            if e.reason in PERMANENT_FAILURES:
                event.failed_reason = e.reason
                session.add(event)
                session.commit()
            continue
        unsubscribe = unsubscribe_url(event, base_url)
        item = (event, {"from": from_addr, "to": [to_addr],
                        "subject": subject, "text": body,
                        "headers": {  # RFC 8058 one-click unsubscribe
                            "List-Unsubscribe": f"<{unsubscribe}>",
                            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                        }})
        if event.batch_key:
            keyed.setdefault(event.batch_key, []).append(item)
        else:
            fresh.append(item)
    pending = [*keyed.values(),
               *(fresh[i:i + BATCH_SIZE] for i in range(0, len(fresh), BATCH_SIZE))]
    while pending:
        batch = pending.pop(0)
        try:
            _deliver(session, api_key, batch, sleep)
            sent += len(batch)
        except RateLimitedError as e:
            errors[e.reason] += len(batch) + sum(len(b) for b in pending)
            break
        except EmailError as e:
            if len(batch) > 1 and e.reason == BATCH_REJECTED:
                # all-or-nothing validation: nothing went out, so one bad
                # address must not hold back the rest; retry one at a time
                for event, _ in batch:
                    event.batch_key = None
                    session.add(event)
                session.commit()
                pending[:0] = [[one] for one in batch]
            else:
                errors[e.reason] += len(batch)
                _count_attempt(session, batch)
    return _result(sent, errors)


def _count_attempt(session: Session, batch) -> None:
    for event, _ in batch:
        event.attempts += 1
        if event.attempts >= MAX_SEND_ATTEMPTS:
            event.failed_reason = GAVE_UP
        session.add(event)
    session.commit()


def _result(sent: int, errors: Counter) -> dict:
    return {"sent": sent, "failed": sum(errors.values()), "errors": dict(errors)}


def _idempotency_key(batch) -> str:
    """Stable for the same events and body; the event ids keep two events
    with identical emails from being deduped into one delivery."""
    emails = [email for _, email in batch]
    blob = json.dumps({"events": [str(event.id) for event, _ in batch], "emails": emails},
                      sort_keys=True)
    return f"sb-{hashlib.sha256(blob.encode()).hexdigest()}"


def _tag_batch(session: Session, batch) -> None:
    key = f"{BATCH_PREFIX}{uuid.uuid4()}"
    for event, _ in batch:
        event.batch_key = key
        session.add(event)
    session.commit()  # before the request: a lost response must resend this batch


def _deliver(session: Session, api_key: str, batch, sleep) -> None:
    emails = [email for _, email in batch]
    legacy = batch[0][0].batch_key or ""
    if legacy.startswith(LEGACY_BATCH_PREFIX):
        try:
            send_batch(api_key, emails, legacy, sleep=sleep)
        except EmailError as e:
            if e.reason != KEY_CONFLICT:
                raise
            # the body changed since the legacy attempt, so that key can't be
            # reused; like any changed body, it goes out under a fresh key
            _tag_batch(session, batch)
            send_batch(api_key, emails, _idempotency_key(batch), sleep=sleep)
    else:
        if len(batch) > 1 and batch[0][0].batch_key is None:
            _tag_batch(session, batch)
        send_batch(api_key, emails, _idempotency_key(batch), sleep=sleep)
    now = datetime.now(timezone.utc)
    for event, _ in batch:
        event.sent = True
        event.sent_at = now
        session.add(event)
    session.commit()


def course_of_chunk(session: Session, chunk) -> object | None:
    resource = session.get(Resource, chunk.resource_id)
    topic = session.get(Topic, resource.topic_id) if resource else None
    return session.get(Course, topic.course_id) if topic else None
