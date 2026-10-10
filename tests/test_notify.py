"""Phase 6 tests: batching, threshold, dedupe, delivery (fake sender)."""

import io
import re
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

import app.notify as notify
from app.main import app
from app.models import (
    Chunk,
    Course,
    NotificationEvent,
    QuizItem,
    Resource,
    ReviewLog,
    Topic,
    User,
)
from tests.dbutil import TEST_DATABASE_URL, make_engine


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _course_with_items(s: Session, n_chunks=2, code="CS 301"):
    user = User(email="s@x.edu")
    s.add(user)
    s.commit()
    course = Course(user_id=user.id, source="moodle", source_id="c1",
                    name="Data Structures", code=code)
    s.add(course)
    s.commit()
    topic = Topic(course_id=course.id, source_id="t1", title="T")
    s.add(topic)
    s.commit()
    res = Resource(topic_id=topic.id, source="moodle", source_id="r1",
                   type="file", title="R", status="extracted", extracted_text="t")
    s.add(res)
    s.commit()
    for i in range(n_chunks):
        chunk = Chunk(resource_id=res.id, title=f"C{i}", content="t", order=i)
        s.add(chunk)
        s.commit()
        s.add(QuizItem(chunk_id=chunk.id, question=f"Q{i}?", question_type="mcq",
                       options=["a", "b", "c", "d"], correct_answer="0",
                       difficulty="recall", generation_key=f"g{i}"))
    s.commit()
    s.refresh(user)
    s.refresh(course)
    return user, course


def test_new_material_batched_per_course(session):
    user, course = _course_with_items(session)
    event = notify.enqueue_new_material(session, course.id, 5)
    assert event.type == "new_material"
    assert event.payload == {"course": "Data Structures", "code": "CS 301",
                             "course_id": str(course.id), "new_items": 5}
    assert event.user_id == user.id and event.sent is False
    assert notify.enqueue_new_material(session, course.id, 0) is None


def test_new_material_coalesces_into_queued_event(session):
    """Each slice of a sliced pipeline job adds to one email, not one each."""
    user, course = _course_with_items(session)
    other = Course(user_id=user.id, source="moodle", source_id="c2",
                   name="Algorithms", code="CS 302")
    session.add(other)
    session.commit()
    first = notify.enqueue_new_material(session, course.id, 5)
    created = first.created_at
    again = notify.enqueue_new_material(session, course.id, 3)
    assert again.id == first.id and again.payload["new_items"] == 8
    assert notify._aware(again.created_at) >= notify._aware(created)  # expiry restarts
    separate = notify.enqueue_new_material(session, other.id, 2)
    assert separate.id != first.id  # per course
    assert len(session.exec(select(NotificationEvent)).all()) == 2


def test_new_material_does_not_coalesce_into_tried_or_sent_events(session, monkeypatch):
    user, course = _course_with_items(session)
    tried = notify.enqueue_new_material(session, course.id, 5)
    tried.attempts = 1  # may have been delivered with its old count
    session.add(tried)
    session.commit()
    assert notify.enqueue_new_material(session, course.id, 3).id != tried.id
    _fake_batches(monkeypatch)
    notify.send_pending(session, "key", "from@x")
    later = notify.enqueue_new_material(session, course.id, 1)
    assert later.sent is False and later.payload["new_items"] == 1


def test_review_due_not_resent_hourly_after_delivery(session, monkeypatch):
    _fake_batches(monkeypatch)
    user, _ = _course_with_items(session, n_chunks=3)  # 3 due >= threshold
    first = notify.check_review_due(session, user.id, threshold=3)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 1
    # the hourly worker runs again with the same backlog: no new email
    assert notify.check_review_due(session, user.id, threshold=3) is None
    # after the cooldown the (still due) user is reminded again
    first.sent_at = first.sent_at - notify.REVIEW_DUE_COOLDOWN
    session.add(first)
    session.commit()
    again = notify.check_review_due(session, user.id, threshold=3)
    assert again is not None and again.id != first.id


def test_review_due_cooldown_starts_at_delivery(session, monkeypatch):
    _fake_batches(monkeypatch)
    user, _ = _course_with_items(session, n_chunks=3)
    first = notify.check_review_due(session, user.id, threshold=3)
    # queued for longer than the cooldown (no API key, outage, ...)
    first.created_at = first.created_at - notify.REVIEW_DUE_COOLDOWN * 2
    session.add(first)
    session.commit()
    assert notify.send_pending(session, "key", "from@x")["sent"] == 1
    # the next hourly run must not send a second reminder for the same backlog
    assert notify.check_review_due(session, user.id, threshold=3) is None


def test_review_due_cooldown_falls_back_to_created_at(session):
    user, _ = _course_with_items(session, n_chunks=3)
    first = notify.check_review_due(session, user.id, threshold=3)
    first.sent = True  # delivered before sent_at was recorded
    session.add(first)
    session.commit()
    assert notify.check_review_due(session, user.id, threshold=3) is None
    first.created_at = first.created_at - notify.REVIEW_DUE_COOLDOWN
    session.add(first)
    session.commit()
    assert notify.check_review_due(session, user.id, threshold=3) is not None


def test_review_due_threshold_and_dedupe(session):
    user, _ = _course_with_items(session, n_chunks=2)  # 2 due < 3
    assert notify.check_review_due(session, user.id) is None
    # one more item in the same course -> 3 due
    chunk = session.exec(select(Chunk)).first()
    session.add(QuizItem(chunk_id=chunk.id, question="Q3?",
                         question_type="mcq", options=["a", "b", "c", "d"],
                         correct_answer="0", difficulty="recall",
                         generation_key="g-extra"))
    session.commit()
    event = notify.check_review_due(session, user.id, threshold=3)
    assert event is not None and event.payload["due_count"] >= 3
    assert notify.check_review_due(session, user.id, threshold=3) is None  # deduped


class _Calls(list):
    """Emails of each send_batch call; `.keys` holds the Idempotency-Keys."""

    def __init__(self):
        super().__init__()
        self.keys = []


def _fake_batches(monkeypatch, fail=lambda emails: None, real_due=False):
    """Record each send_batch call; `fail` may raise to simulate errors.
    Queued reminders count as still due (most tests here queue them for
    users with no questions) unless `real_due`."""
    calls = _Calls()
    if not real_due:
        monkeypatch.setattr(notify, "_still_due", lambda session, user: True)

    def fake(api_key, emails, idempotency_key, sleep=None):
        fail(emails)
        calls.append(emails)
        calls.keys.append(idempotency_key)

    monkeypatch.setattr(notify, "send_batch", fake)
    return calls


def test_send_marks_sent_and_renders(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    user, course = _course_with_items(session, n_chunks=1)
    notify.enqueue_new_material(session, course.id, 2)
    out = notify.send_pending(session, "key", "from@x", "to@x")
    assert out == {"sent": 1, "failed": 0, "errors": {}}
    [[email]] = calls
    assert email["subject"] == "New study material: CS 301"
    assert "2 new quiz items" in email["text"]
    assert email["to"] == ["s@x.edu"] and email["from"] == "from@x"
    event = session.exec(select(NotificationEvent)).one()
    assert event.sent is True and event.sent_at is not None


def test_send_failure_stays_queued(session, monkeypatch):
    def boom(emails):
        raise notify.EmailError("resend returned 500", "http_500")

    _fake_batches(monkeypatch, boom)
    user, course = _course_with_items(session, n_chunks=1)
    notify.enqueue_new_material(session, course.id, 2)
    out = notify.send_pending(session, "key", "from@x", "to@x")
    assert out == {"sent": 0, "failed": 1, "errors": {"http_500": 1}}
    assert session.exec(select(NotificationEvent)).one().sent is False


def test_send_goes_to_event_owner_not_fallback(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    other = User(email="other@x.edu")
    session.add(other)
    session.commit()
    session.refresh(other)
    owned = Course(user_id=other.id, source="moodle", source_id="owned",
                   name="Owned", code="OWN")
    session.add(owned)
    session.commit()
    session.refresh(owned)
    event = notify.enqueue_new_material(session, owned.id, 3)
    assert event.user_id == other.id
    out = notify.send_pending(session, "key", "from@x", "fallback@x")
    assert out == {"sent": 1, "failed": 0, "errors": {}}
    assert [e["to"] for e in calls[0]] == [["other@x.edu"]]


@pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="the users FK rules out a missing user")
def test_send_without_recipient_fails_once(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    # stale reference: user row gone, no fallback -> cannot deliver
    orphan = NotificationEvent(user_id=uuid.UUID(int=0),
                               type="review_due", payload={"due_count": 9})
    session.add(orphan)
    session.commit()
    out = notify.send_pending(session, "key", "from@x", "")
    assert out == {"sent": 0, "failed": 1, "errors": {"no_recipient": 1}}
    assert calls == []
    event = session.exec(select(NotificationEvent)).one()
    assert event.sent is False and event.failed_reason == "no_recipient"
    # out of the queue: later passes don't re-render it forever
    assert notify.send_pending(session, "key", "from@x", "") == {
        "sent": 0, "failed": 0, "errors": {}}


def test_render_review_due():
    event = NotificationEvent(user_id="00000000-0000-0000-0000-000000000000",
                              type="review_due", payload={"due_count": 5})
    subject, body = notify.render(event, "https://sb.example.com/")
    assert subject == "5 reviews due" and "5 quiz items" in body
    assert "https://sb.example.com/review" in body and "localhost" not in body


def test_render_singular_counts():
    uid = "00000000-0000-0000-0000-000000000000"
    subject, body = notify.render(NotificationEvent(
        user_id=uid, type="review_due", payload={"due_count": 1}), "https://sb/")
    assert subject == "1 review due" and "You have 1 quiz item due" in body
    _, body = notify.render(NotificationEvent(
        user_id=uid, type="new_material",
        payload={"new_items": 1, "course": "Maths"}), "https://sb/")
    assert body.startswith("1 new quiz item from Maths.")


def test_render_uses_app_base_url_setting(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "app_base_url", "https://prod.example.com")
    event = NotificationEvent(user_id=uuid.UUID(int=0), type="new_material",
                              payload={"course": "DS", "code": "CS", "new_items": 2})
    _, body = notify.render(event)
    assert "Review them: https://prod.example.com/review\n" in body
    assert "https://prod.example.com/unsubscribe/" in body


def test_unowned_course_enqueues_nothing_and_creates_no_user(session):
    course = Course(source="moodle", source_id="orphan", name="Orphan", code="ORP")
    session.add(course)
    session.commit()
    assert notify.enqueue_new_material(session, course.id, 4) is None
    assert notify.enqueue_new_material(session, uuid.uuid4(), 4) is None  # missing
    assert session.exec(select(User)).all() == []
    assert session.exec(select(NotificationEvent)).all() == []


def test_review_due_counts_all_due_items(session, monkeypatch):
    from app import grade
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 100)
    user, _ = _course_with_items(session, n_chunks=25)  # > due_items' default page
    event = notify.check_review_due(session, user.id, threshold=3)
    assert event.payload == {"due_count": 25}


def test_review_due_respects_daily_new_cap(session):
    from app import grade
    user, _ = _course_with_items(session, n_chunks=25)
    event = notify.check_review_due(session, user.id, threshold=3)
    assert event.payload == {"due_count": grade.NEW_ITEMS_PER_DAY}


def _owned_events(session, n):
    user = User(email="u@x.edu")
    session.add(user)
    session.commit()
    for i in range(n):
        session.add(NotificationEvent(user_id=user.id, type="review_due",
                                      payload={"due_count": i}))
    session.commit()


def test_send_batches_requests(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    _owned_events(session, notify.BATCH_SIZE + 5)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": notify.BATCH_SIZE + 5, "failed": 0, "errors": {}}
    assert [len(c) for c in calls] == [notify.BATCH_SIZE, 5]
    assert all(e.sent for e in session.exec(select(NotificationEvent)))


def test_bad_email_in_batch_does_not_block_others(session, monkeypatch):
    def reject_bad(emails):
        if any(e["subject"] == "1 review due" for e in emails):
            raise notify.EmailError("resend returned 422", "http_422:validation_error")

    calls = _fake_batches(monkeypatch, reject_bad)
    _owned_events(session, 3)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 2, "failed": 1,
                   "errors": {"http_422:validation_error": 1}}
    assert [len(c) for c in calls] == [1, 1]  # the batch retried one by one
    unsent = session.exec(
        select(NotificationEvent).where(NotificationEvent.sent == False)  # noqa: E712
    ).all()
    assert [e.payload["due_count"] for e in unsent] == [1]


def test_rate_limit_stops_run_and_leaves_rest_queued(session, monkeypatch):
    def limited(emails):
        raise notify.RateLimitedError("429")

    _fake_batches(monkeypatch, limited)
    _owned_events(session, notify.BATCH_SIZE + 1)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": notify.BATCH_SIZE + 1,
                   "errors": {"rate_limited": notify.BATCH_SIZE + 1}}
    assert not any(e.sent for e in session.exec(select(NotificationEvent)))


def test_items_added_during_delivery_get_their_own_event(session, monkeypatch):
    """A merge while the email is in flight must not land on an event about
    to be marked sent, where its items would never be mailed."""
    _, course = _course_with_items(session)
    first = notify.enqueue_new_material(session, course.id, 5)
    added = []

    def slice_finishes_mid_send(emails):
        added.append(notify.enqueue_new_material(session, course.id, 3))

    calls = _fake_batches(monkeypatch, slice_finishes_mid_send)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 1
    assert "5 new quiz items" in calls[0][0]["text"]
    [later] = added
    assert later.id != first.id and later.payload["new_items"] == 3
    assert later.sent is False  # mailed on the next pass
    calls = _fake_batches(monkeypatch)
    notify.send_pending(session, "key", "from@x")
    assert "3 new quiz items" in calls[0][0]["text"]


def test_expired_event_is_not_merged_into(session):
    _, course = _course_with_items(session)
    old = notify.enqueue_new_material(session, course.id, 4)
    old.created_at = datetime.now(timezone.utc) - notify.NEW_MATERIAL_MAX_AGE - timedelta(hours=1)
    session.add(old)
    session.commit()
    fresh = notify.enqueue_new_material(session, course.id, 2)
    assert fresh.id != old.id and fresh.payload["new_items"] == 2
    assert old.payload["new_items"] == 4


def test_old_new_material_expires_without_api_key(session):
    _, course = _course_with_items(session)
    old = notify.enqueue_new_material(session, course.id, 4)
    old.created_at = datetime.now(timezone.utc) - notify.NEW_MATERIAL_MAX_AGE - timedelta(hours=1)
    session.add(old)
    session.commit()
    _owned_events(session, 1)
    assert notify.send_pending(session, "", "from@x") == {
        "sent": 0, "failed": 2, "errors": {"expired": 1, "no_api_key": 1}}
    assert old.failed_reason == "expired"


def test_failing_event_gives_up_after_max_attempts(session, monkeypatch):
    def bad_key(emails):
        raise notify.EmailError("resend returned http_401", "http_401")

    calls = _fake_batches(monkeypatch, bad_key)
    _owned_events(session, 1)
    for _ in range(notify.MAX_SEND_ATTEMPTS - 1):
        assert notify.send_pending(session, "key", "from@x")["errors"] == {"http_401": 1}
    event = session.exec(select(NotificationEvent)).one()
    assert event.attempts == notify.MAX_SEND_ATTEMPTS - 1 and event.failed_reason is None
    notify.send_pending(session, "key", "from@x")
    assert event.failed_reason == notify.GAVE_UP and event.sent is False
    # out of the queue: no more requests for it
    calls = _fake_batches(monkeypatch)
    assert notify.send_pending(session, "key", "from@x") == {
        "sent": 0, "failed": 0, "errors": {}}
    assert calls == []


def test_rate_limit_and_missing_key_cost_no_attempt(session, monkeypatch):
    _fake_batches(monkeypatch, lambda emails: (_ for _ in ()).throw(
        notify.RateLimitedError("429")))
    _owned_events(session, 1)
    notify.send_pending(session, "key", "from@x")
    notify.send_pending(session, "", "from@x")
    assert session.exec(select(NotificationEvent)).one().attempts == 0


def test_old_new_material_expires_instead_of_sending(session, monkeypatch):
    """After an outage, days-old 'new material' news is dropped, not burst out."""
    calls = _fake_batches(monkeypatch)
    _, course = _course_with_items(session)
    old = notify.enqueue_new_material(session, course.id, 4)
    old.created_at = datetime.now(timezone.utc) - notify.NEW_MATERIAL_MAX_AGE - timedelta(hours=1)
    session.add(old)
    session.commit()
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": 1, "errors": {"expired": 1}}
    assert calls == [] and old.failed_reason == "expired"


def test_no_api_key_sends_nothing(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    _owned_events(session, 2)
    assert notify.send_pending(session, "", "from@x") == {
        "sent": 0, "failed": 2, "errors": {"no_api_key": 2}}
    assert calls == []


def _http_429(retry_after="1.5"):
    return urllib.error.HTTPError(
        notify.RESEND_BATCH_URL, 429, "Too Many Requests",
        {"Retry-After": retry_after}, io.BytesIO(b""),
    )


def _raise_on_urlopen(monkeypatch, err):
    def urlopen(req, timeout):
        raise err

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)


class _OK:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_batch_backs_off_on_429_then_succeeds(monkeypatch):
    replies = [_http_429(), _http_429("nope"), _OK()]

    def urlopen(req, timeout):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    slept = []
    notify.send_batch("key", [{"to": ["a@x"]}], "k", sleep=slept.append)
    assert slept == [1.5, 2.0] and replies == []  # Retry-After, then 2**attempt


def test_send_batch_gives_up_after_retries(monkeypatch):
    def urlopen(req, timeout):
        raise _http_429("0")

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    slept = []
    with pytest.raises(notify.RateLimitedError):
        notify.send_batch("key", [{"to": ["a@x"]}], "k", sleep=slept.append)
    assert len(slept) == notify.MAX_RETRIES


def test_retry_after_parses_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    wait = notify._retry_after(_http_429(format_datetime(when, usegmt=True)), 0)
    assert 25 <= wait <= 30
    past = format_datetime(when - timedelta(hours=1), usegmt=True)
    assert notify._retry_after(_http_429(past), 0) == 0.0


def test_send_batch_gives_up_when_retry_after_exceeds_cap(monkeypatch):
    _raise_on_urlopen(monkeypatch, _http_429(str(notify.MAX_RETRY_WAIT + 1)))
    slept = []
    with pytest.raises(notify.RateLimitedError) as exc:
        notify.send_batch("key", [{"to": ["a@x"]}], "k", sleep=slept.append)
    assert slept == [] and exc.value.reason == "rate_limited"


def test_send_batch_reason_uses_resend_error_name_not_message(monkeypatch):
    body = b'{"statusCode":422,"name":"validation_error","message":"bad a@x.edu"}'
    _raise_on_urlopen(monkeypatch, urllib.error.HTTPError(
        notify.RESEND_BATCH_URL, 422, "Unprocessable", {}, io.BytesIO(body)))
    with pytest.raises(notify.EmailError) as exc:
        notify.send_batch("key", [{"to": ["a@x.edu"]}], "k")
    assert exc.value.reason == "http_422:validation_error"
    assert "a@x.edu" not in str(exc.value)


def test_send_batch_reason_drops_unexpected_error_names(monkeypatch):
    body = b'{"name":"Bad <script> a@x.edu"}'
    _raise_on_urlopen(monkeypatch, urllib.error.HTTPError(
        notify.RESEND_BATCH_URL, 500, "Server Error", {}, io.BytesIO(body)))
    with pytest.raises(notify.EmailError) as exc:
        notify.send_batch("key", [{"to": ["a@x"]}], "k")
    assert exc.value.reason == "http_500"


def test_single_send_uses_stable_event_key(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    _owned_events(session, 1)
    notify.send_pending(session, "key", "from@x")
    event = session.exec(select(NotificationEvent)).one()
    assert calls.keys == [notify._idempotency_key([(event, calls[0][0])])]
    assert event.batch_key is None


def test_ambiguous_batch_failure_resends_same_batch_same_key(session, monkeypatch):
    keys = []

    def lost(emails):
        raise notify.EmailError("send failed: timed out", "network:TimeoutError")

    calls = _fake_batches(monkeypatch, lost)
    real_key = notify._idempotency_key
    monkeypatch.setattr(notify, "_idempotency_key", lambda b: keys.append(real_key(b)) or keys[-1])
    _owned_events(session, 3)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": 3, "errors": {"network:TimeoutError": 3}}
    assert len(calls.keys) == 0  # fake raised before recording
    events = session.exec(select(NotificationEvent)).all()
    [key] = {e.batch_key for e in events}
    assert key and key.startswith(notify.BATCH_PREFIX)  # persisted, not split into singles
    [first_key] = keys

    # a newer event arrives; the next pass resends the old batch unchanged
    session.add(NotificationEvent(user_id=events[0].user_id, type="review_due",
                                  payload={"due_count": 99}))
    session.commit()
    calls = _fake_batches(monkeypatch)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 4, "failed": 0, "errors": {}}
    assert calls.keys[0] == first_key  # same events, same body -> same key
    assert calls.keys[1] != first_key
    assert [len(c) for c in calls] == [3, 1]


def _newest(session):
    return session.exec(
        select(NotificationEvent).order_by(NotificationEvent.created_at.desc())
    ).first()


def test_server_error_does_not_split_batch(session, monkeypatch):
    def boom(emails):
        raise notify.EmailError("resend returned http_500", "http_500")

    attempts = []
    _fake_batches(monkeypatch, lambda emails: (attempts.append(len(emails)), boom(emails)))
    _owned_events(session, 3)
    out = notify.send_pending(session, "key", "from@x")
    assert out["errors"] == {"http_500": 3} and attempts == [3]  # no singles


def test_validation_split_clears_batch_key(session, monkeypatch):
    def reject_batches(emails):
        if len(emails) > 1:
            raise notify.EmailError("422", notify.BATCH_REJECTED)

    calls = _fake_batches(monkeypatch, reject_batches)
    _owned_events(session, 2)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 2
    events = session.exec(select(NotificationEvent)).all()
    assert all(e.batch_key is None for e in events)
    assert len(set(calls.keys)) == 2  # one key per single send


def test_send_batch_sends_idempotency_key(monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append(req.get_header("Idempotency-key"))
        return _OK()

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    notify.send_batch("key", [{"to": ["a@x"]}], "batch-abc")
    assert seen == ["batch-abc"]


def test_changed_batch_body_gets_new_idempotency_key(session, monkeypatch):
    """A kept batch resent with a different body must not reuse the old key
    (Resend answers 409 for 24h); an unchanged resend must reuse it."""
    def lost(emails):
        raise notify.EmailError("send failed: timed out", "network:TimeoutError")

    keys = []
    real_key = notify._idempotency_key
    monkeypatch.setattr(notify, "_idempotency_key",
                        lambda b: keys.append(real_key(b)) or keys[-1])
    _fake_batches(monkeypatch, lost)
    _owned_events(session, 2)
    notify.send_pending(session, "key", "from@x", base_url="https://a.example")
    notify.send_pending(session, "key", "from@x", base_url="https://a.example")
    notify.send_pending(session, "key", "from@x", base_url="https://b.example")
    assert keys[0] == keys[1] and keys[2] != keys[0]
    assert len({e.batch_key for e in session.exec(select(NotificationEvent))}) == 1


def test_identical_emails_for_different_events_get_different_keys():
    body = {"from": "f@x", "to": ["a@x"], "subject": "s", "text": "t"}
    one = NotificationEvent(user_id=uuid.UUID(int=0), type="review_due", payload={})
    two = NotificationEvent(user_id=uuid.UUID(int=0), type="review_due", payload={})
    assert notify._idempotency_key([(one, body)]) != notify._idempotency_key([(two, body)])


def _legacy_batch(session, n=2, key="batch-legacy"):
    """A batch persisted (and maybe delivered) before payload-derived keys."""
    _owned_events(session, n)
    events = session.exec(select(NotificationEvent)).all()
    for event in events:
        event.batch_key = key
        session.add(event)
    session.commit()
    return events


def _record_keys(monkeypatch, fail=lambda key: None):
    seen = []

    def fake(api_key, emails, key, sleep=None):
        seen.append(key)
        fail(key)

    monkeypatch.setattr(notify, "send_batch", fake)
    monkeypatch.setattr(notify, "_still_due", lambda session, user: True)
    return seen


def test_legacy_batch_resent_with_its_original_key(session, monkeypatch):
    seen = _record_keys(monkeypatch)
    events = _legacy_batch(session)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 2
    assert seen == ["batch-legacy"]  # Resend dedupes the lost first attempt
    assert all(e.sent and e.batch_key == "batch-legacy" for e in events)


def test_legacy_batch_keeps_key_across_ambiguous_failures(session, monkeypatch):
    def lost(key):
        raise notify.EmailError("send failed: timed out", "network:TimeoutError")

    seen = _record_keys(monkeypatch, lost)
    events = _legacy_batch(session)
    notify.send_pending(session, "key", "from@x")
    notify.send_pending(session, "key", "from@x")
    assert seen == ["batch-legacy", "batch-legacy"]
    assert all(not e.sent and e.batch_key == "batch-legacy" for e in events)


def test_legacy_batch_moves_to_new_key_on_payload_conflict(session, monkeypatch):
    def conflict(key):
        if key == "batch-legacy":
            raise notify.EmailError("409", notify.KEY_CONFLICT)

    seen = _record_keys(monkeypatch, conflict)
    events = _legacy_batch(session)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 2
    assert seen[0] == "batch-legacy" and seen[1].startswith("sb-")
    [key] = {e.batch_key for e in events}
    assert key.startswith(notify.BATCH_PREFIX)


def test_legacy_batch_other_409_keeps_legacy_key(session, monkeypatch):
    def busy(key):
        raise notify.EmailError("409", "http_409:concurrent_idempotent_requests")

    seen = _record_keys(monkeypatch, busy)
    events = _legacy_batch(session)
    assert notify.send_pending(session, "key", "from@x")["failed"] == 2
    assert seen == ["batch-legacy"]
    assert all(e.batch_key == "batch-legacy" for e in events)


def _set(s: Session, obj, **fields):
    for k, v in fields.items():
        setattr(obj, k, v)
    s.add(obj)
    s.commit()


def test_review_due_skips_opted_out_user(session):
    user, _ = _course_with_items(session, n_chunks=3)
    _set(session, user, notify_email=False)
    assert notify.check_review_due(session, user.id, threshold=3) is None
    _set(session, user, notify_email=True)
    assert notify.check_review_due(session, user.id, threshold=3) is not None


def test_new_material_skips_opted_out_owner(session):
    user, course = _course_with_items(session)
    _set(session, user, notify_email=False)
    assert notify.enqueue_new_material(session, course.id, 5) is None


def test_archived_course_sends_no_news_and_no_review_reminder(session):
    user, course = _course_with_items(session, n_chunks=3)
    _set(session, course, archived=True)
    assert notify.enqueue_new_material(session, course.id, 5) is None
    assert notify.check_review_due(session, user.id, threshold=3) is None
    _set(session, course, archived=False)
    assert notify.check_review_due(session, user.id, threshold=3) is not None


def test_review_due_pauses_for_inactive_users(session):
    user, course = _course_with_items(session, n_chunks=3)
    old = datetime.now(timezone.utc) - notify.REVIEW_DUE_ACTIVE_WINDOW - timedelta(days=1)
    # synced once and never studied: no reminder after the window
    _set(session, user, created_at=old)
    assert notify.check_review_due(session, user.id, threshold=3) is None
    # answered long ago: still inactive
    item = session.exec(select(QuizItem)).first()
    log = ReviewLog(user_id=user.id, quiz_item_id=item.id, answered_at=old,
                    verdict="correct")
    session.add(log)
    session.commit()
    assert notify.check_review_due(session, user.id, threshold=3) is None
    # a recent answer makes them active again
    _set(session, log, answered_at=datetime.now(timezone.utc))
    assert notify.check_review_due(session, user.id, threshold=3) is not None


def test_queued_events_of_opted_out_user_fail(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    user, course = _course_with_items(session, n_chunks=3)
    notify.enqueue_new_material(session, course.id, 3)
    _set(session, user, notify_email=False)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": 1, "errors": {"opted_out": 1}}
    assert calls == []
    assert session.exec(select(NotificationEvent)).one().failed_reason == "opted_out"
    # opting back in later doesn't resurrect the stale event...
    _set(session, user, notify_email=True)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 0
    # ...and a failed review_due doesn't block a new one
    failed = NotificationEvent(user_id=user.id, type="review_due",
                               payload={"due_count": 3}, failed_reason="opted_out")
    session.add(failed)
    session.commit()
    assert notify.check_review_due(session, user.id, threshold=3) is not None


def test_emails_carry_one_click_unsubscribe(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    user, course = _course_with_items(session)
    notify.enqueue_new_material(session, course.id, 2)
    notify.send_pending(session, "key", "from@x", base_url="https://sb.example.com")
    [[email]] = calls
    url = email["headers"]["List-Unsubscribe"].strip("<>")
    assert url.startswith("https://sb.example.com/unsubscribe/") and url in email["text"]
    assert email["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    token = url.rsplit("/", 1)[1]
    assert notify.unsubscribe_user(session, token).id == user.id


def test_unsubscribe_token_is_signed(session):
    user, _ = _course_with_items(session)
    token = notify.unsubscribe_token(user.id, "secret-a")
    assert notify.unsubscribe_user(session, token, "secret-a").id == user.id
    assert notify.unsubscribe_user(session, token, "secret-b") is None
    assert notify.unsubscribe_user(session, token[:-2] + "xx", "secret-a") is None
    assert notify.unsubscribe_user(session, "garbage", "secret-a") is None


def _notify_email(testapp) -> bool:
    with testapp["Session"]() as s:
        return s.get(User, testapp["user_id"]).notify_email


def test_settings_toggles_notifications(testapp):
    client = testapp["client"]
    page = client.get("/settings/moodle").text
    assert 'name="notify_email" checked' in page
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    r = client.post("/settings/notifications", data={"csrf_token": token})  # unchecked
    assert "notifications turned off" in r.text and not _notify_email(testapp)
    client.post("/settings/notifications", data={"csrf_token": token, "notify_email": "on"})
    assert _notify_email(testapp)
    assert client.post("/settings/notifications",
                       data={"csrf_token": "bogus"}).status_code == 403


def test_unsubscribe_link(testapp):
    client = TestClient(app)  # signed out: the token is the only credential
    url = f"/unsubscribe/{notify.unsubscribe_token(testapp['user_id'])}"
    page = client.get(url)
    # GET only confirms, so link scanners can't unsubscribe anyone
    assert page.status_code == 200 and "Unsubscribe?" in page.text
    assert _notify_email(testapp)
    # RFC 8058 one-click POST, as a mail client sends it
    r = client.post(url, data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and "unsubscribed" in r.text
    assert not _notify_email(testapp)
    assert client.post(url).status_code == 200  # idempotent
    assert client.get("/unsubscribe/forged").status_code == 404
    assert client.post("/unsubscribe/forged").status_code == 404


def test_stale_review_due_fails_at_delivery(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    user, _ = _course_with_items(session, n_chunks=3)
    event = notify.check_review_due(session, user.id, threshold=3)
    # delivery delayed past the window: the user went inactive meanwhile
    old = datetime.now(timezone.utc) - notify.REVIEW_DUE_ACTIVE_WINDOW - timedelta(days=1)
    _set(session, user, created_at=old)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": 1, "errors": {"inactive": 1}}
    assert calls == [] and event.failed_reason == "inactive"


def test_events_queued_before_archiving_are_not_sent(session, monkeypatch):
    calls = _fake_batches(monkeypatch, real_due=True)
    user, course = _course_with_items(session, n_chunks=3)
    news = notify.enqueue_new_material(session, course.id, 5)
    reminder = notify.check_review_due(session, user.id, threshold=3)
    assert news is not None and reminder is not None
    _set(session, course, archived=True)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 0, "failed": 2, "errors": {"archived": 1, "not_due": 1}}
    assert calls == []
    assert (news.failed_reason, reminder.failed_reason) == ("archived", "not_due")


def test_queued_events_of_an_active_course_still_send(session, monkeypatch):
    calls = _fake_batches(monkeypatch, real_due=True)
    user, course = _course_with_items(session, n_chunks=3)
    notify.enqueue_new_material(session, course.id, 5)
    notify.check_review_due(session, user.id, threshold=3)
    out = notify.send_pending(session, "key", "from@x")
    assert out == {"sent": 2, "failed": 0, "errors": {}} and len(calls) == 1


def test_opt_out_fails_queued_events(session, monkeypatch):
    calls = _fake_batches(monkeypatch)
    user, course = _course_with_items(session, n_chunks=3)
    notify.enqueue_new_material(session, course.id, 3)
    notify.opt_out(session, user)
    # back on before the worker runs: what was queued while off stays unsent
    _set(session, user, notify_email=True)
    assert notify.send_pending(session, "key", "from@x")["sent"] == 0
    assert calls == []
    assert session.exec(select(NotificationEvent)).one().failed_reason == "opted_out"


def _queue_event(testapp):
    with testapp["Session"]() as s:
        s.add(NotificationEvent(user_id=testapp["user_id"], type="review_due",
                                payload={"due_count": 3}))
        s.commit()


def _failed_reasons(testapp):
    with testapp["Session"]() as s:
        return [e.failed_reason for e in s.exec(select(NotificationEvent))]


def test_settings_off_then_on_drops_queued_events(testapp):
    client = testapp["client"]
    _queue_event(testapp)
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      client.get("/settings/moodle").text).group(1)
    client.post("/settings/notifications", data={"csrf_token": token})
    client.post("/settings/notifications", data={"csrf_token": token, "notify_email": "on"})
    assert _notify_email(testapp) and _failed_reasons(testapp) == ["opted_out"]


def test_unsubscribe_fails_queued_events(testapp):
    _queue_event(testapp)
    url = f"/unsubscribe/{notify.unsubscribe_token(testapp['user_id'])}"
    assert TestClient(app).post(url).status_code == 200
    assert _failed_reasons(testapp) == ["opted_out"]


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="advisory locks are Postgres-only")
def test_racing_first_enqueues_make_one_event():
    import threading

    engine = make_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        _, course = _course_with_items(s)
        course_id = course.id
    holder = Session(engine)
    # a first enqueue that holds the per-user lock and hasn't committed yet
    owner = holder.get(Course, course_id).user_id
    notify._lock_new_material(holder, owner)
    holder.add(NotificationEvent(user_id=owner, type="new_material", payload={
        "course": "Data Structures", "code": "CS 301",
        "course_id": str(course_id), "new_items": 5}))
    holder.flush()

    def racer():
        with Session(engine) as s:
            notify.enqueue_new_material(s, course_id, 3)

    t = threading.Thread(target=racer)
    t.start()
    t.join(0.5)
    assert t.is_alive()  # waits on the lock instead of inserting its own
    holder.commit()
    holder.close()
    t.join(10)
    with Session(engine) as s:
        [event] = s.exec(select(NotificationEvent)).all()
        assert event.payload["new_items"] == 8
