"""Phase 5 tests: review queue, answering (MCQ), stats."""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.grade import due_count, due_items
from app.models import Chunk, Course, ItemFlag, QuizItem, Resource, ReviewState, Topic, User
from tests.dbutil import make_engine


def _seed(Session):
    with Session() as s:
        user = User(email="s@x.edu")
        s.add(user)
        # owned by the signed-in fixture user (test@x.edu); unowned courses are hidden
        owner = s.exec(select(User).where(User.email == "test@x.edu")).one()
        course = Course(user_id=owner.id, source="moodle", source_id="c1", name="C")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t1", title="T")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r1",
                       type="file", title="R", status="extracted", extracted_text="t")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch", content="t", order=0)
        s.add(chunk)
        s.commit()
        mcq = QuizItem(chunk_id=chunk.id, question="Which?", question_type="mcq",
                       options=["a", "b", "c", "d"], correct_answer="1",
                       explanation="Because b.", difficulty="recall",
                       generation_key="g1")
        s.add(mcq)
        s.commit()
        s.refresh(mcq)
        return str(mcq.id)


def _token(client):
    page = client.get("/review/take")
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def test_review_flow(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)

    r = client.get("/review")
    assert r.status_code == 200 and "Which?" in r.text

    r = client.get("/review/take")
    assert r.status_code == 200 and 'name="answer"' in r.text

    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": _token(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/review/{item_id}/result"
    r = client.get(r.headers["location"])
    assert r.status_code == 200 and "Correct" in r.text and "Because b." in r.text

    r = client.get("/review")
    assert "due" in r.text and "Which?" not in r.text  # answered, not due

    r = client.get("/stats")
    assert r.status_code == 200 and "100.0%" in r.text and "1 day" in r.text


def test_review_wrong_answer(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "0", "csrf_token": _token(client)})
    assert "Incorrect" in r.text


def test_stats_streak_and_empty(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    r = client.get("/stats")
    assert "—" in r.text  # no answers yet
    # streak via direct helper
    from app.stats import compute_stats
    with Session() as s:
        from sqlmodel import select
        u = s.exec(select(User)).first()
        assert compute_stats(s, u.id)["streak_days"] == 0


def _other_users_item(Session):
    with Session() as s:
        other = User(email="o@x.edu")
        s.add(other)
        s.commit()
        course = Course(user_id=other.id, source="moodle", source_id="c2", name="Theirs")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t2", title="T2")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r2",
                       type="file", title="R2")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch2", content="t", order=0)
        s.add(chunk)
        s.commit()
        item = QuizItem(chunk_id=chunk.id, question="Theirs?", question_type="mcq",
                        options=["a", "b"], correct_answer="0", generation_key="g9")
        s.add(item)
        s.commit()
        s.refresh(item)
        return str(item.id)


def _short(Session):
    with Session() as s:
        from sqlmodel import select
        chunk = s.exec(select(Chunk)).first()
        item = QuizItem(chunk_id=chunk.id, question="Explain?", question_type="short_answer",
                        correct_answer="because", grading_criteria="says because",
                        generation_key="g2")
        s.add(item)
        s.commit()
        s.refresh(item)
        return str(item.id)


def test_stats_items_total_scoped(testapp):
    Session = testapp["Session"]
    _seed(Session)
    _other_users_item(Session)
    from app.stats import compute_stats
    with Session() as s:
        assert compute_stats(s, testapp["user_id"])["items_total"] == 1


def test_cannot_answer_other_users_item(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    theirs = _other_users_item(Session)
    r = client.post(f"/review/{theirs}/answer",
                    data={"answer": "0", "csrf_token": _token(client)})
    assert r.status_code == 404


def test_invalid_mcq_answer_rejected(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "9", "csrf_token": _token(client)})
    assert r.status_code == 400 and "Pick one of the listed options" in r.text
    with Session() as s:
        from sqlmodel import select
        assert s.exec(select(ReviewState)).first() is None


def test_grader_failure_keeps_answer(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    from app.llm import LLMError

    class Down:
        def __init__(self, *a, **kw):
            pass

        def complete_json(self, *a, **k):
            raise LLMError("upstream 502")

    monkeypatch.setattr(main, "LLMClient", Down)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "because <reasons>", "csrf_token": _token(client)})
    assert r.status_code == 503
    assert "grader is unavailable" in r.text
    assert "because &lt;reasons&gt;</textarea>" in r.text
    with Session() as s:
        from sqlmodel import select
        assert s.exec(select(ReviewState)).first() is None


def test_grader_misconfigured_keeps_answer(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    monkeypatch.setattr(main.settings, "llm_api_key", "")
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "my answer", "csrf_token": _token(client)})
    assert r.status_code == 503 and "my answer</textarea>" in r.text


def test_replayed_answer_not_regraded(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    token = _token(client)
    client.post(f"/review/{item_id}/answer", data={"answer": "1", "csrf_token": token})
    # back + resubmit with a different answer: shows the recorded result
    r = client.post(f"/review/{item_id}/answer", data={"answer": "0", "csrf_token": token})
    assert r.status_code == 200 and "Correct" in r.text
    with Session() as s:
        state = s.exec(select(ReviewState)).one()
        assert state.repetitions == 1 and state.lapses == 0


def test_result_page_needs_a_result(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.get(f"/review/{item_id}/result", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/take"


def test_long_short_answer_rejected(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    calls = []

    class Counting:
        def __init__(self, *a, **kw):
            pass

        def complete_json(self, *a, **k):
            calls.append(a)
            return {"correct": True}

    monkeypatch.setattr(main, "LLMClient", Counting)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "x" * 4001, "csrf_token": _token(client)})
    assert calls == []  # rejected before grading
    assert r.status_code == 400 and "limited to 4,000 characters" in r.text
    assert 'maxlength="4000"' in r.text


def test_grader_gets_short_timeout(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    seen = {}

    class Fast:
        def __init__(self, *a, **kw):
            seen.update(kw)

        def complete_json(self, *a, **k):
            return {"correct": True, "feedback": "Nice."}

    monkeypatch.setattr(main, "LLMClient", Fast)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "because", "csrf_token": _token(client)})
    assert "Correct" in r.text and "Nice." in r.text
    assert seen["timeout"] <= 30 and seen["max_attempts"] <= 2


def test_long_feedback_survives_the_redirect(testapp, monkeypatch):
    # feedback lives in ReviewState, not the cookie-backed session
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main
    feedback = "🙂 Great answer. " * 300  # ~5k chars, ~50k once JSON-escaped

    class Chatty:
        def __init__(self, *a, **kw):
            pass

        def complete_json(self, *a, **k):
            return {"correct": True, "partial_credit": 1.0, "feedback": feedback}

    monkeypatch.setattr(main, "LLMClient", Chatty)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "because", "csrf_token": _token(client)})
    assert r.status_code == 200 and "Correct" in r.text
    assert feedback.strip() in r.text
    assert len(client.cookies.get("session", "")) < 4000


def test_new_item_past_cap_redirects_to_queue(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    from app import grade
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 0)
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/settings/moodle").text)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": token.group(1)}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(r.headers["location"], follow_redirects=False).headers["location"] \
        == "/review/take"
    with Session() as s:
        assert s.exec(select(ReviewState)).first() is None


def _answer_again(Session):
    """Make the (only) answered item due again."""
    with Session() as s:
        state = s.exec(select(ReviewState)).one()
        state.next_review_date = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.add(state)
        s.commit()


def test_stats_count_every_answer_not_just_the_latest(testapp):
    from app.models import ReviewLog
    from app.stats import compute_stats

    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    client.post(f"/review/{item_id}/answer", data={"answer": "1", "csrf_token": _token(client)})
    _answer_again(Session)
    client.post(f"/review/{item_id}/answer", data={"answer": "0", "csrf_token": _token(client)})
    with Session() as s:
        logs = s.exec(select(ReviewLog).order_by(ReviewLog.answered_at)).all()
        assert [(log.verdict, log.partial_credit) for log in logs] == [
            ("correct", 1.0), ("incorrect", 0.0)]
        stats = compute_stats(s, testapp["user_id"])
    # the second answer overwrote ReviewState.last_result; history still has both
    assert stats["answered"] == 2 and stats["accuracy"] == 0.5


def _log(s, user_id, at):
    from app.models import ReviewLog
    s.add(ReviewLog(user_id=user_id, verdict="correct", partial_credit=1.0, answered_at=at))


def test_streak_keeps_days_whose_items_were_answered_again(testapp):
    from app.stats import compute_stats

    Session, user_id = testapp["Session"], testapp["user_id"]
    now = datetime.now(timezone.utc)
    with Session() as s:
        for days_ago in (0, 1, 2):  # the same item re-answered on three days
            _log(s, user_id, now - timedelta(days=days_ago))
        s.commit()
        assert compute_stats(s, user_id, tz=timezone.utc)["streak_days"] == 3


def test_streak_days_are_local_dates():
    from zoneinfo import ZoneInfo

    from sqlmodel import Session, SQLModel

    from app.stats import compute_stats

    nairobi = ZoneInfo("Africa/Nairobi")  # UTC+3
    engine = make_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="s@x.edu")
        s.add(user)
        s.commit()
        # 00:30 local on the 30th is still the 29th in UTC
        _log(s, user.id, datetime(2026, 9, 30, 0, 30, tzinfo=nairobi))
        _log(s, user.id, datetime(2026, 9, 29, 12, 0, tzinfo=nairobi))
        s.commit()
        now = datetime(2026, 9, 30, 1, 0, tzinfo=nairobi)
        assert compute_stats(s, user.id, tz=nairobi, now=now)["streak_days"] == 2
        assert compute_stats(s, user.id, tz=timezone.utc, now=now)["streak_days"] == 1


def _reveal(html):
    """(classes, tags) per option on the result page."""
    rows = re.findall(r'<li class="reveal-option([^"]*)">(.*?)</li>', html, re.S)
    return [(cls.split(), re.findall(r'class="reveal-tag">([^<]+)<', body))
            for cls, body in rows]


def test_result_marks_key_and_wrong_pick(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "2", "csrf_token": _token(client)})
    assert r.status_code == 200 and "Incorrect" in r.text
    assert _reveal(r.text) == [
        ([], []),
        (["is-key"], ["Correct answer"]),
        (["is-wrong-pick"], ["Your answer"]),
        ([], []),
    ]
    assert 'name="answer"' not in r.text  # read-only: no radios to re-pick


def test_result_correct_pick_is_the_key(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": _token(client)})
    assert _reveal(r.text)[1] == (["is-key"], ["Correct answer", "Your answer"])
    assert "is-wrong-pick" not in r.text


def test_result_before_last_answer_was_stored(testapp):
    # rows answered before migration 0017 have no last_answer: show the key only
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    client.post(f"/review/{item_id}/answer", data={"answer": "0", "csrf_token": _token(client)})
    with Session() as s:
        state = s.exec(select(ReviewState)).one()
        state.last_answer = None
        s.add(state)
        s.commit()
    r = client.get(f"/review/{item_id}/result")
    assert r.status_code == 200
    assert [cls for cls, _ in _reveal(r.text)] == [[], ["is-key"], [], []]
    assert "Your answer" not in r.text


def test_result_shows_short_answer_and_reference(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    item_id = _short(Session)
    from app import main

    class Partial:
        def __init__(self, *a, **kw):
            pass

        def complete_json(self, *a, **k):
            return {"correct": False, "partial_credit": 0.5, "feedback": "Half there."}

    monkeypatch.setattr(main, "LLMClient", Partial)
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "<b>since</b>", "csrf_token": _token(client)})
    assert r.status_code == 200 and "Half there." in r.text
    assert re.search(r"Your answer</h3>\s*<p[^>]*>&lt;b&gt;since&lt;/b&gt;</p>", r.text)
    assert re.search(r"Reference answer</h3>\s*<p[^>]*>because</p>", r.text)
    with Session() as s:
        assert s.exec(select(ReviewState)).one().last_answer == "<b>since</b>"


def test_result_links_to_source_passage(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    with Session() as s:
        chunk = s.exec(select(Chunk)).one()
        rid, cid = chunk.resource_id, chunk.id
    page = client.get("/review/take").text
    assert "?chunk=" not in page  # the passage would give the answer away
    r = client.post(f"/review/{item_id}/answer",
                    data={"answer": "1", "csrf_token": _token(client)})
    assert f'href="/resources/{rid}?chunk={cid}#source"' in r.text
    assert "R — Ch" in r.text


def _resource_page(testapp, text, start, end, content="chunk body"):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        course = Course(user_id=testapp["user_id"], source="moodle", source_id="c1", name="C")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t1", title="T")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r1", type="file",
                       title="R", status="extracted", extracted_text=text)
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch", content=content, order=0,
                      start_char=start, end_char=end)
        s.add(chunk)
        s.commit()
        rid, cid = res.id, chunk.id
    return client.get(f"/resources/{rid}?chunk={cid}").text, cid


def test_source_passage_highlighted_with_context(testapp):
    from app.main import SOURCE_CONTEXT_CHARS

    text = "x" * 1000 + "<the passage>" + "y" * 1000
    page, cid = _resource_page(testapp, text, 1000, 1013)
    m = re.search(r'<pre class="text source-text">(.*?)<mark class="source-mark">(.*?)</mark>'
                  r'(.*?)</pre>', page, re.S)
    assert m.group(1) == "…" + "x" * SOURCE_CONTEXT_CHARS
    assert m.group(2) == "&lt;the passage&gt;"  # escaped
    assert m.group(3) == "y" * SOURCE_CONTEXT_CHARS + "…"
    assert f'class="chunk-card is-source" id="chunk-{cid}"' in page


@pytest.mark.parametrize("start,end", [(None, None), (5, 50)])
def test_source_passage_without_offsets_shows_chunk(testapp, start, end):
    # offsets reset by a text refresh, or past the end of the current text
    page, _ = _resource_page(testapp, "short", start, end, content="stored chunk")
    assert '<mark class="source-mark">stored chunk</mark>' in page
    assert "be located in the current text" in page


def test_unknown_source_chunk_ignored(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _resource_page(testapp, "abc", 0, 3)
    with Session() as s:
        rid = s.exec(select(Resource)).one().id
    r = client.get(f"/resources/{rid}?chunk=00000000-0000-0000-0000-000000000000")
    assert r.status_code == 200 and "source-mark" not in r.text


@pytest.mark.parametrize("days, gap", [(40, None), (100, None), (70, 45), (40, 31), (40, 32)])
def test_streak_runs_past_the_first_window(testapp, days, gap):
    from app.stats import STREAK_WINDOW_DAYS, compute_stats

    assert days > STREAK_WINDOW_DAYS
    Session, user_id = testapp["Session"], testapp["user_id"]
    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    with Session() as s:
        for days_ago in range(days):
            if days_ago != gap:
                _log(s, user_id, now - timedelta(days=days_ago))
        s.commit()
        streak = compute_stats(s, user_id, tz=timezone.utc, now=now)["streak_days"]
    assert streak == (gap if gap is not None else days)


def _seed_overdue(testapp, n: int) -> str:
    """`n` overdue MCQs for the signed-in user; returns the course id."""
    with testapp["Session"]() as s:
        course = Course(user_id=testapp["user_id"], source="moodle", source_id="cq",
                        name="Queue")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="tq", title="T")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="rq",
                       type="file", title="R", status="extracted", extracted_text="t")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch", content="t", order=0)
        s.add(chunk)
        s.commit()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        for i in range(n):
            item = QuizItem(chunk_id=chunk.id, question=f"Q{i}?", question_type="mcq",
                            options=["a", "b"], correct_answer="0", generation_key=f"q{i}")
            s.add(item)
            s.commit()
            s.add(ReviewState(user_id=testapp["user_id"], quiz_item_id=item.id,
                              next_review_date=past, interval_days=1, repetitions=1))
        s.commit()
        return str(course.id)


def test_review_page_counts_every_due_item(testapp):
    _seed_overdue(testapp, 35)
    page = testapp["client"].get("/review").text
    assert "<strong>35</strong> questions due" in page
    assert "Showing the next 20 of 35" in page
    assert page.count('class="grow question"') == 20


def test_review_page_without_truncation_note(testapp):
    _seed_overdue(testapp, 3)
    page = testapp["client"].get("/review").text
    assert "<strong>3</strong> questions due" in page and "Showing the next" not in page


def test_nav_badge_on_every_page(testapp):
    client = testapp["client"]
    cid = _seed_overdue(testapp, 2)
    with testapp["Session"]() as s:
        rid = str(s.exec(select(Resource)).one().id)
    badge = '<span class="nav-count">2</span>'
    for path in ["/", f"/courses/{cid}", f"/resources/{rid}", "/review",
                 "/review/take", "/stats", "/settings/moodle"]:
        page = client.get(path).text
        assert badge in page, path
        assert "test@x.edu" in page and 'href="/settings/moodle"' in page, path


def test_error_page_keeps_the_account_header(testapp):
    _seed_overdue(testapp, 2)
    r = testapp["client"].get("/courses/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert "test@x.edu" in r.text and 'href="/settings/moodle"' in r.text
    assert '<span class="nav-count">2</span>' in r.text


def test_stats_schedule_copy_matches_srs():
    from pathlib import Path

    from app.srs import next_interval_days

    first, reps, ease = next_interval_days(5, 0, 2.5, 0)
    second, reps, ease = next_interval_days(5, reps, ease, first)
    third, _, _ = next_interval_days(5, reps, ease, second)
    assert (first, second) == (1, 3) and 7 <= third <= 9  # "about a week"
    copy = (Path(__file__).parent.parent / "templates" / "stats.html").read_text()
    assert f"({first} day, then {second}, then about a week, growing each time)" in copy


def test_nav_badge_failure_still_renders_the_page(testapp, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database down")

    _seed_overdue(testapp, 2)
    monkeypatch.setattr("app.main.due_count", broken)
    r = testapp["client"].get("/stats")
    assert r.status_code == 200
    assert "test@x.edu" in r.text and "nav-count" not in r.text
    assert "<dt>Due now</dt>\n    <dd>—</dd>" in r.text  # unknown, not 0
    r = testapp["client"].get("/review")
    assert r.status_code == 200 and "nav-count" not in r.text
    assert "<strong>2</strong> questions due" in r.text  # falls back to the queue


def _course_id(Session):
    with Session() as s:
        return str(s.exec(select(Course)).one().id)


def test_archived_course_leaves_courses_and_review_but_keeps_history(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    cid = _course_id(Session)
    with Session() as s:  # a second item, so one stays due after answering the first
        chunk = s.exec(select(Chunk)).one()
        s.add(QuizItem(chunk_id=chunk.id, question="Why?", question_type="mcq",
                       options=["a", "b"], correct_answer="0", generation_key="g2"))
        s.commit()
    token = _token(client)
    client.post(f"/review/{item_id}/answer", data={"answer": "1", "csrf_token": token})
    with Session() as s:
        assert due_count(s, testapp["user_id"]) == 1

    r = client.post(f"/courses/{cid}/archive", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"

    home = client.get("/").text
    assert 'id="courses-list"' not in home and "All your courses are archived" in home
    assert "Archived (1)" in home and f'href="/courses/{cid}"' in home
    assert "due for review" not in home  # the nav badge and the strip are gone
    assert "Why?" not in client.get("/review").text
    with Session() as s:
        assert due_count(s, testapp["user_id"]) == 0
        assert due_items(s, testapp["user_id"]) == []
        # history stays: the answer, its schedule and the items themselves
        assert s.exec(select(ReviewState)).one().last_result == "correct"
        assert len(s.exec(select(QuizItem)).all()) == 2
    stats = client.get("/stats").text
    assert "100.0%" in stats

    # the course page still opens, and offers the way back
    page = client.get(f"/courses/{cid}").text
    assert f'action="/courses/{cid}/unarchive"' in page and "Archived:" in page
    # an item already on screen when the course was archived can still be answered
    result = client.get(f"/review/{item_id}/result")
    assert result.status_code == 200

    r = client.post(f"/courses/{cid}/unarchive", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/courses/{cid}"
    home = client.get("/").text
    assert 'id="courses-list"' in home and "Archived (" not in home
    assert "Why?" in client.get("/review").text
    assert f'action="/courses/{cid}/archive"' in client.get(f"/courses/{cid}").text


def test_archive_needs_csrf_token_and_ownership(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    cid = _course_id(Session)
    token = _token(client)
    assert client.post(f"/courses/{cid}/archive", data={}).status_code == 403
    with Session() as s:
        other = s.exec(select(User).where(User.email == "s@x.edu")).one()
        theirs = Course(user_id=other.id, source="moodle", source_id="c2", name="Theirs")
        s.add(theirs)
        s.commit()
        theirs_id = theirs.id
    for action in ("archive", "unarchive"):
        r = client.post(f"/courses/{theirs_id}/{action}", data={"csrf_token": token})
        assert r.status_code == 404
    with Session() as s:
        assert [c.archived for c in s.exec(select(Course)).all()] == [False, False]


def test_archived_course_listed_apart_from_active_ones(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        s.add_all([
            Course(user_id=testapp["user_id"], source="moodle", source_id="old",
                   name="Last Semester", code="OLD 101", archived=True),
            Course(user_id=testapp["user_id"], source="moodle", source_id="new",
                   name="This Semester"),
        ])
        s.commit()
    home = client.get("/").text
    active, _, archived = home.partition('<details class="archived">')
    assert "This Semester" in active and "Last Semester" not in active
    assert "1 course<" in active
    assert "Archived (1)" in archived and "Last Semester" in archived
    assert "All your courses are archived" not in home


def _item_ids(Session) -> dict:
    with Session() as s:
        return {i.question: str(i.id) for i in s.exec(select(QuizItem)).all()}


def _served(client) -> str:
    """The question /review/take is showing."""
    page = client.get("/review/take").text
    return re.search(r'<h1 class="quiz-question">(.*?)</h1>', page).group(1)


def test_skip_sends_the_question_to_the_back_of_the_queue(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed_overdue(testapp, 3)
    ids = _item_ids(Session)
    token = _token(client)
    page = client.get("/review/take").text
    assert "Q0?" in page and f'action="/review/{ids["Q0?"]}/skip"' in page

    r = client.post(f"/review/{ids['Q0?']}/skip", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/take"
    assert _served(client) == "Q1?"
    with Session() as s:
        assert [i.question for i in due_items(s, testapp["user_id"])] == ["Q1?", "Q2?", "Q0?"]
        assert due_count(s, testapp["user_id"]) == 3  # skipped, still due

    # skipping the rest brings the first one round again, oldest skip first
    client.post(f"/review/{ids['Q1?']}/skip", data={"csrf_token": token})
    client.post(f"/review/{ids['Q2?']}/skip", data={"csrf_token": token})
    assert _served(client) == "Q0?"
    client.post(f"/review/{ids['Q0?']}/skip", data={"csrf_token": token})  # twice: no 500
    assert _served(client) == "Q1?"
    with Session() as s:  # the schedule itself never moved
        assert {st.repetitions for st in s.exec(select(ReviewState)).all()} == {1}
        assert {st.answered_at for st in s.exec(select(ReviewState)).all()} == {None}

    # and a skipped question can still be answered
    r = client.post(f"/review/{ids['Q1?']}/answer", data={"answer": "0", "csrf_token": token})
    assert "Correct" in r.text
    assert _served(client) == "Q2?"


def test_skip_lasts_only_for_the_day(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed_overdue(testapp, 2)
    ids = _item_ids(Session)
    with Session() as s:
        s.add(ItemFlag(user_id=testapp["user_id"], quiz_item_id=uuid.UUID(ids["Q0?"]),
                       skipped_at=datetime.now(timezone.utc) - timedelta(days=2)))
        s.commit()
    assert _served(client) == "Q0?"


def test_skipped_new_item_goes_behind_other_new_items_and_keeps_the_cap(testapp, monkeypatch):
    client, Session = testapp["client"], testapp["Session"]
    first = _seed(Session)
    with Session() as s:
        chunk = s.exec(select(Chunk)).one()
        s.add(QuizItem(chunk_id=chunk.id, question="Why?", question_type="mcq",
                       options=["a", "b"], correct_answer="0", generation_key="g2"))
        s.commit()
    token = _token(client)
    assert _served(client) == "Which?"
    client.post(f"/review/{first}/skip", data={"csrf_token": token})
    assert _served(client) == "Why?"
    with Session() as s:
        assert [i.question for i in due_items(s, testapp["user_id"])] == ["Why?", "Which?"]
        assert s.exec(select(ReviewState)).all() == []  # a skip doesn't start the item

    monkeypatch.setattr("app.grade.NEW_ITEMS_PER_DAY", 1)
    with Session() as s:  # one slot: the unskipped item takes it
        assert [i.question for i in due_items(s, testapp["user_id"])] == ["Why?"]
        assert due_count(s, testapp["user_id"]) == 1


def test_suspend_drops_the_question_and_restore_brings_it_back(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed_overdue(testapp, 2)
    ids = _item_ids(Session)
    token = _token(client)
    page = client.get("/review/take").text
    assert f'action="/review/{ids["Q0?"]}/suspend"' in page
    assert 'value="wrong_answer"' in page and "suspended question" not in client.get("/review").text

    r = client.post(f"/review/{ids['Q0?']}/suspend",
                    data={"reason": "wrong_answer", "note": "  b is right\x00 ",
                          "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/take"
    assert _served(client) == "Q1?"
    with Session() as s:
        assert [i.question for i in due_items(s, testapp["user_id"])] == ["Q1?"]
        assert due_count(s, testapp["user_id"]) == 1
        flag = s.exec(select(ItemFlag)).one()
        assert (flag.reason, flag.note) == ("wrong_answer", "b is right")
        assert flag.suspended_at is not None
        assert len(s.exec(select(ReviewState)).all()) == 2  # its schedule is kept

    queue = client.get("/review").text
    assert "Q0?" not in queue and '<a href="/review/suspended">1 suspended question</a>' in queue
    listed = client.get("/review/suspended").text
    assert "Q0?" in listed and "The marked answer is wrong — b is right" in listed
    assert f'action="/review/{ids["Q0?"]}/restore"' in listed and "Q1?" not in listed

    r = client.post(f"/review/{ids['Q0?']}/restore", data={"csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/suspended"
    assert "No suspended questions" in client.get("/review/suspended").text
    with Session() as s:
        assert due_count(s, testapp["user_id"]) == 2
        flag = s.exec(select(ItemFlag)).one()
        assert (flag.suspended_at, flag.reason, flag.note) == (None, None, None)
    assert "suspended question" not in client.get("/review").text


def test_suspending_the_last_question_empties_the_queue(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    token = _token(client)
    r = client.post(f"/review/{item_id}/suspend", data={"reason": "unclear", "csrf_token": token})
    assert r.status_code == 200 and "Nothing due" in r.text
    assert "1 suspended question" in r.text  # the way back, even with nothing due


def test_suspend_files_an_unknown_reason_under_other_and_caps_the_note(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    client.post(f"/review/{item_id}/suspend",
                data={"reason": "<script>", "note": "x" * 2000, "csrf_token": _token(client)})
    with Session() as s:
        flag = s.exec(select(ItemFlag)).one()
        assert flag.reason == "other" and len(flag.note) == 500
    assert "Something else — xxx" in client.get("/review/suspended").text


def test_result_page_can_report_but_not_skip(testapp):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    token = _token(client)
    r = client.post(f"/review/{item_id}/answer", data={"answer": "0", "csrf_token": token})
    assert "Incorrect" in r.text and f'action="/review/{item_id}/suspend"' in r.text
    assert "/skip" not in r.text
    client.post(f"/review/{item_id}/suspend", data={"reason": "wrong_answer", "csrf_token": token})
    with Session() as s:  # suspended while not yet due: it never comes back by itself
        state = s.exec(select(ReviewState)).one()
        state.next_review_date = datetime.now(timezone.utc) - timedelta(days=1)
        s.add(state)
        s.commit()
        assert due_count(s, testapp["user_id"]) == 0
        assert due_items(s, testapp["user_id"]) == []


@pytest.mark.parametrize("action", ["skip", "suspend", "restore"])
def test_flagging_needs_csrf_token_and_ownership(testapp, action):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    theirs = _other_users_item(Session)
    token = _token(client)
    assert client.post(f"/review/{item_id}/{action}", data={}).status_code == 403
    r = client.post(f"/review/{theirs}/{action}", data={"csrf_token": token})
    assert r.status_code == 404
    r = client.post(f"/review/{uuid.uuid4()}/{action}", data={"csrf_token": token})
    assert r.status_code == 404
    with Session() as s:
        assert s.exec(select(ItemFlag)).all() == []


def test_suspended_list_is_scoped_to_the_user(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    theirs = _other_users_item(Session)
    with Session() as s:
        other = s.exec(select(User).where(User.email == "s@x.edu")).one()
        s.add(ItemFlag(user_id=other.id, quiz_item_id=uuid.UUID(str(theirs)),
                       suspended_at=datetime.now(timezone.utc), reason="other"))
        s.commit()
        assert due_count(s, testapp["user_id"]) == 1  # their flag isn't ours
    assert "No suspended questions" in client.get("/review/suspended").text


def test_purging_a_resource_drops_its_flags(testapp):
    from app.sync import _purge_derived

    Session = testapp["Session"]
    item_id = _seed(Session)
    with Session() as s:
        s.add(ItemFlag(user_id=testapp["user_id"], quiz_item_id=uuid.UUID(item_id),
                       suspended_at=datetime.now(timezone.utc), reason="other"))
        s.commit()
        _purge_derived(s, s.exec(select(Resource)).one().id)
        s.commit()
        assert s.exec(select(ItemFlag)).all() == []


def _two_courses(testapp) -> tuple[str, str, str]:
    """Course "C" with the new item "Which?" and course "Queue" with two
    overdue items. Returns (C's id, Queue's id, "Which?"'s id)."""
    item_id = _seed(testapp["Session"])
    c = _course_id(testapp["Session"])
    return c, _seed_overdue(testapp, 2), item_id


def test_review_one_course_stays_in_that_course(testapp):
    client, Session = testapp["client"], testapp["Session"]
    c, queue, _ = _two_courses(testapp)
    ids = _item_ids(Session)

    page = client.get(f"/review?course={queue}").text
    assert "<strong>2</strong> questions due in Queue" in page
    assert "Q0?" in page and "Q1?" in page and "Which?" not in page
    assert f'href="/review/take?course={queue}"' in page
    # the nav badge still counts every course
    assert '<span class="nav-count">3</span>' in page
    page = client.get(f"/review?course={c}").text
    assert "<strong>1</strong> question due in C" in page
    assert "Which?" in page and "Q0?" not in page

    take = client.get(f"/review/take?course={queue}").text
    assert '<h1 class="quiz-question">Q0?</h1>' in take
    assert "1 more due in Queue after this one" in take
    assert f'href="/review?course={queue}"' in take
    for action in ("answer", "skip", "suspend"):
        assert f'action="/review/{ids["Q0?"]}/{action}?course={queue}"' in take

    token = _token(client)
    r = client.post(f"/review/{ids['Q0?']}/answer?course={queue}",
                    data={"answer": "0", "csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/review/{ids['Q0?']}/result?course={queue}"
    result = client.get(r.headers["location"]).text
    assert "1 more due in Queue after this one" in result  # Q1
    assert f'id="next-question-btn" href="/review/take?course={queue}"' in result

    r = client.post(f"/review/{ids['Q1?']}/skip?course={queue}",
                    data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/review/take?course={queue}"
    r = client.post(f"/review/{ids['Q1?']}/suspend?course={queue}",
                    data={"csrf_token": token, "reason": "unclear"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/review/take?course={queue}"

    # the course is done while another still has a question due
    done = client.get(r.headers["location"]).text
    assert "Nothing due in Queue" in done and 'href="/review"' in done
    assert "Which?" not in done
    assert '<h1 class="quiz-question">Which?</h1>' in client.get("/review/take").text


def test_course_queue_and_count_helpers(testapp):
    c, queue, _ = _two_courses(testapp)
    uid = testapp["user_id"]
    with testapp["Session"]() as s:
        assert [i.question for i in due_items(s, uid, course_id=uuid.UUID(queue))] \
            == ["Q0?", "Q1?"]
        assert [i.question for i in due_items(s, uid, course_id=uuid.UUID(c))] == ["Which?"]
        assert due_count(s, uid, uuid.UUID(queue)) == 2
        assert due_count(s, uid, uuid.UUID(c)) == 1
        assert due_count(s, uid) == 3 and len(due_items(s, uid)) == 3


def test_course_review_keeps_the_daily_new_item_cap(testapp, monkeypatch):
    """The cap is the user's: a new item answered in one course spends the
    slot a single-course review of another would have used."""
    client, Session = testapp["client"], testapp["Session"]
    c, _, item_id = _two_courses(testapp)
    with Session() as s:  # a second course with a new item
        other = Course(user_id=testapp["user_id"], source="moodle", source_id="c3", name="D")
        s.add(other)
        s.commit()
        topic = Topic(course_id=other.id, source_id="t3", title="T")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r3", type="file",
                       title="R", status="extracted", extracted_text="t")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch", content="t", order=0)
        s.add(chunk)
        s.commit()
        s.add(QuizItem(chunk_id=chunk.id, question="Other?", question_type="mcq",
                       options=["a", "b"], correct_answer="0", generation_key="g3"))
        s.commit()
        d = other.id
    monkeypatch.setattr("app.grade.NEW_ITEMS_PER_DAY", 1)
    uid = testapp["user_id"]
    with Session() as s:
        assert [i.question for i in due_items(s, uid, course_id=d)] == ["Other?"]
        assert due_count(s, uid, d) == 1
    client.post(f"/review/{item_id}/answer?course={c}",
                data={"answer": "1", "csrf_token": _token(client)})
    with Session() as s:
        assert due_items(s, uid, course_id=d) == []
        assert due_count(s, uid, d) == 0
    assert "Nothing due in D" in client.get(f"/review/take?course={d}").text


def test_course_review_skipped_item_goes_last_within_the_course(testapp):
    client, Session = testapp["client"], testapp["Session"]
    _, queue, _ = _two_courses(testapp)
    ids = _item_ids(Session)
    client.post(f"/review/{ids['Q0?']}/skip?course={queue}",
                data={"csrf_token": _token(client)})
    with Session() as s:
        assert [i.question for i in due_items(s, testapp["user_id"],
                                              course_id=uuid.UUID(queue))] == ["Q1?", "Q0?"]


@pytest.mark.parametrize("path", ["/review", "/review/take"])
def test_course_review_needs_the_users_own_course(testapp, path):
    client, Session = testapp["client"], testapp["Session"]
    _seed(Session)
    _other_users_item(Session)
    with Session() as s:
        theirs = s.exec(select(Course).where(Course.name == "Theirs")).one().id
    assert client.get(f"{path}?course={theirs}").status_code == 404
    assert client.get(f"{path}?course={uuid.uuid4()}").status_code == 404
    assert client.get(f"{path}?course=nope").status_code == 400


@pytest.mark.parametrize("action", ["answer", "skip", "suspend"])
def test_course_review_posts_refuse_a_foreign_course(testapp, action):
    client, Session = testapp["client"], testapp["Session"]
    item_id = _seed(Session)
    _other_users_item(Session)
    with Session() as s:
        theirs = s.exec(select(Course).where(Course.name == "Theirs")).one().id
    r = client.post(f"/review/{item_id}/{action}?course={theirs}",
                    data={"answer": "1", "reason": "other", "csrf_token": _token(client)},
                    follow_redirects=False)
    assert r.status_code == 404
    with Session() as s:  # nothing was recorded
        assert s.exec(select(ReviewState)).all() == []
        assert s.exec(select(ItemFlag)).all() == []


def test_result_without_a_result_returns_to_the_course_review(testapp):
    client = testapp["client"]
    c, _, item_id = _two_courses(testapp)
    r = client.get(f"/review/{item_id}/result?course={c}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/review/take?course={c}"


def test_rejected_answer_keeps_the_course_review(testapp):
    client = testapp["client"]
    c, _, item_id = _two_courses(testapp)
    r = client.post(f"/review/{item_id}/answer?course={c}",
                    data={"answer": "9", "csrf_token": _token(client)})
    assert r.status_code == 400
    assert f'action="/review/{item_id}/answer?course={c}"' in r.text


def test_course_page_links_to_its_review(testapp):
    client = testapp["client"]
    c, queue, item_id = _two_courses(testapp)
    page = client.get(f"/courses/{queue}").text
    assert "<strong>2</strong> questions due in this course" in page
    assert f'href="/review/take?course={queue}"' in page
    assert "<strong>1</strong> question due in this course" in client.get(f"/courses/{c}").text

    token = _token(client)
    client.post(f"/review/{item_id}/answer", data={"answer": "1", "csrf_token": token})
    assert "due in this course" not in client.get(f"/courses/{c}").text  # nothing due

    client.post(f"/courses/{queue}/archive", data={"csrf_token": token})
    assert "due in this course" not in client.get(f"/courses/{queue}").text
    page = client.get(f"/review?course={queue}").text
    assert "Nothing due in Queue" in page and "This course is archived" in page
