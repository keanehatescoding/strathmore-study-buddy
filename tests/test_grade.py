"""Phase 4 tests: MCQ grading, short-answer flow, SM-2 progression, due queue."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from app import grade
from app.grade import (
    InvalidAnswer,
    NotDue,
    due_count,
    due_items,
    grade_short_answer,
    submit_answer,
    user_owns_item,
)
from app.models import Chunk, Course, QuizItem, Resource, ReviewState, Topic, User
from tests.dbutil import TEST_DATABASE_URL, make_engine


class FakeLLM:
    def __init__(self, partial=0.8):
        self.partial = partial

    def complete_json(self, system, user, temperature=0.0):
        return {"correct": self.partial >= 0.6, "partial_credit": self.partial,
                "feedback": "Good effort."}


class ReplyLLM:
    """Returns a canned grader reply and counts calls."""

    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def complete_json(self, system, user, temperature=0.0):
        self.calls += 1
        return self.reply


def _make_due(s, user, item):
    state = s.exec(select(ReviewState).where(
        ReviewState.user_id == user.id, ReviewState.quiz_item_id == item.id)).one()
    state.next_review_date = datetime.now(timezone.utc) - timedelta(minutes=1)
    s.add(state)
    s.commit()


@pytest.fixture()
def setup():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="s@x.edu")
        s.add(user)
        s.commit()
        course = Course(user_id=user.id, source="moodle", source_id="c1", name="C")
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
                       difficulty="recall", generation_key="g1")
        short = QuizItem(chunk_id=chunk.id, question="Explain?", question_type="short_answer",
                         correct_answer="because reasons", grading_criteria="says because",
                         difficulty="application", generation_key="g2")
        s.add_all([mcq, short])
        s.commit()
        s.refresh(user)
        s.refresh(mcq)
        s.refresh(short)
        yield s, user, mcq, short


def test_mcq_instant_no_llm(setup):
    s, user, mcq, _ = setup
    assert submit_answer(s, user.id, mcq.id, "1")["correct"] is True
    _make_due(s, user, mcq)
    assert submit_answer(s, user.id, mcq.id, "0")["correct"] is False


def test_mcq_wrong_resets_interval(setup):
    s, user, mcq, _ = setup
    r1 = submit_answer(s, user.id, mcq.id, "1")
    assert (r1["interval_days"], r1["repetitions"]) == (1, 1)
    _make_due(s, user, mcq)
    r2 = submit_answer(s, user.id, mcq.id, "2")
    assert (r2["interval_days"], r2["repetitions"]) == (1, 0)
    state = s.exec(select(ReviewState)).one()
    assert state.lapses == 1 and state.last_result == "incorrect"


def test_short_answer_uses_criteria(setup):
    s, user, _, short = setup
    out = submit_answer(s, user.id, short.id, "because stuff", FakeLLM(0.8))
    assert out["correct"] is True and out["quality"] == 4
    assert out["feedback"] == "Good effort."
    _make_due(s, user, short)
    out = submit_answer(s, user.id, short.id, "dunno", FakeLLM(0.1))
    assert out["correct"] is False and out["verdict"] == "incorrect"


def test_interval_grows_on_streak(setup):
    s, user, mcq, _ = setup
    submit_answer(s, user.id, mcq.id, "1")
    _make_due(s, user, mcq)
    out = submit_answer(s, user.id, mcq.id, "1")
    assert (out["interval_days"], out["repetitions"]) == (3, 2)
    assert out["next_review_date"] > datetime.now(timezone.utc)


def test_due_queue_new_items_after_overdue(setup):
    s, user, mcq, short = setup
    assert [i.id for i in due_items(s, user.id)] == [mcq.id, short.id]
    submit_answer(s, user.id, mcq.id, "1")
    submit_answer(s, user.id, short.id, "x", FakeLLM(1.0))
    assert due_items(s, user.id) == []  # nothing due yet
    # force one overdue
    state = s.exec(select(ReviewState).where(ReviewState.quiz_item_id == mcq.id)).one()
    state.next_review_date = datetime.now(timezone.utc) - timedelta(hours=1)
    s.add(state)
    s.commit()
    assert [i.id for i in due_items(s, user.id)] == [mcq.id]


@pytest.mark.parametrize("bad", ["", "x", "-1", "4", "1.0"])
def test_mcq_rejects_out_of_range_or_garbage(setup, bad):
    s, user, mcq, _ = setup
    with pytest.raises(InvalidAnswer):
        submit_answer(s, user.id, mcq.id, bad)
    assert s.exec(select(ReviewState)).first() is None  # nothing recorded


def test_mcq_tolerates_whitespace(setup):
    s, user, mcq, _ = setup
    assert submit_answer(s, user.id, mcq.id, " 1 ")["correct"] is True


def test_due_scoped_to_own_courses(setup):
    s, user, mcq, short = setup
    other = User(email="o@x.edu")
    s.add(other)
    s.commit()
    course = Course(user_id=other.id, source="moodle", source_id="c2", name="Theirs")
    s.add(course)
    s.commit()
    topic = Topic(course_id=course.id, source_id="t2", title="T2")
    s.add(topic)
    s.commit()
    res = Resource(topic_id=topic.id, source="moodle", source_id="r2", type="file", title="R2")
    s.add(res)
    s.commit()
    chunk = Chunk(resource_id=res.id, title="Ch2", content="t", order=0)
    s.add(chunk)
    s.commit()
    theirs = QuizItem(chunk_id=chunk.id, question="Q?", question_type="mcq",
                      options=["a", "b"], correct_answer="0", generation_key="g3")
    s.add(theirs)
    s.commit()

    assert [i.id for i in due_items(s, user.id)] == [mcq.id, short.id]
    assert due_count(s, user.id) == 2
    assert not user_owns_item(s, user.id, theirs.id)
    assert user_owns_item(s, user.id, mcq.id)
    # the other user sees only their own item
    assert due_count(s, other.id) == 1


def test_unowned_course_items_hidden_from_everyone(setup):
    s, user, mcq, _ = setup
    course = s.get(Course, s.get(Topic, s.get(Resource, s.get(Chunk, mcq.chunk_id)
                                                          .resource_id).topic_id).course_id)
    course.user_id = None
    s.add(course)
    s.commit()
    assert due_count(s, user.id) == 0
    assert not user_owns_item(s, user.id, mcq.id)


def test_due_count_ignores_limit(setup):
    s, user, _, _ = setup
    assert len(due_items(s, user.id, limit=1)) == 1
    assert due_count(s, user.id) == 2


def test_not_due_item_rejected_before_grading(setup):
    s, user, _, short = setup
    llm = ReplyLLM({"correct": True, "partial_credit": 1.0, "feedback": "ok"})
    first = submit_answer(s, user.id, short.id, "because", llm)
    with pytest.raises(NotDue):
        submit_answer(s, user.id, short.id, "because", llm)
    assert llm.calls == 1  # the replay cost no LLM call
    state = s.exec(select(ReviewState)).one()
    assert state.repetitions == 1 and state.interval_days == first["interval_days"]


# On Postgres the user-row lock held through grading makes the second submit
# wait for the first; run on one thread, that wait never ends.
@pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="needs locks that don't block")
def test_concurrent_first_answer_loses_without_500(setup, monkeypatch):
    s, user, mcq, _ = setup
    # another request inserted and answered while this one was grading
    original = grade.grade_mcq

    def racing(item, answer):
        monkeypatch.setattr(grade, "grade_mcq", original)
        with Session(s.get_bind()) as other:
            submit_answer(other, user.id, mcq.id, "1")
        return original(item, answer)

    monkeypatch.setattr(grade, "grade_mcq", racing)
    with pytest.raises(NotDue):
        submit_answer(s, user.id, mcq.id, "1")
    state = s.exec(select(ReviewState)).one()
    assert state.repetitions == 1 and state.lapses == 0


@pytest.mark.parametrize("reply, credit", [
    ({"correct": True, "feedback": "Spot on."}, 1.0),
    ({"correct": "true", "partial_credit": None}, 1.0),
    ({"correct": False}, 0.0),
    ({"correct": "false", "partial_credit": "n/a"}, 0.0),
    ({"correct": True, "partial_credit": 0.4}, 0.4),  # explicit credit wins
])
def test_grader_correct_flag_used_when_credit_missing(setup, reply, credit):
    _, _, _, short = setup
    out = grade_short_answer(ReplyLLM(reply), short, "because")
    assert out["partial_credit"] == credit and out["correct"] is (credit >= 0.6)


def test_correct_without_credit_schedules_a_pass(setup):
    s, user, _, short = setup
    out = submit_answer(s, user.id, short.id, "because",
                        ReplyLLM({"correct": True, "feedback": "Yes."}))
    assert out["quality"] == 5 and out["verdict"] == "correct"
    assert s.exec(select(ReviewState)).one().lapses == 0


@pytest.mark.parametrize("stored", ["1", "1.0", " 1 ", "True"])
def test_mcq_legacy_stored_index(setup, stored):
    s, user, mcq, _ = setup
    mcq.correct_answer = stored
    s.add(mcq)
    s.commit()
    assert submit_answer(s, user.id, mcq.id, "1")["correct"] is True


def test_short_answer_length_capped(setup):
    s, user, _, short = setup
    llm = ReplyLLM({"correct": True, "partial_credit": 1.0})
    with pytest.raises(InvalidAnswer, match="4,000"):
        submit_answer(s, user.id, short.id, "x" * (grade.MAX_ANSWER_CHARS + 1), llm)
    assert llm.calls == 0
    submit_answer(s, user.id, short.id, "x" * grade.MAX_ANSWER_CHARS, llm)


def _add_items(s, chunk_id, n, prefix):
    items = [QuizItem(chunk_id=chunk_id, question=f"{prefix}{i}?", question_type="mcq",
                      options=["a", "b", "c", "d"], correct_answer="0", difficulty="recall",
                      generation_key=f"{prefix}{i:03d}") for i in range(n)]
    s.add_all(items)
    s.commit()
    return items


def test_overdue_not_starved_by_new_backlog(setup):
    s, user, mcq, short = setup
    submit_answer(s, user.id, mcq.id, "1")
    _make_due(s, user, mcq)
    _add_items(s, mcq.chunk_id, 30, "new")
    queue = due_items(s, user.id)
    assert queue[0].id == mcq.id and len(queue) == 20


def test_daily_new_item_cap(setup, monkeypatch):
    s, user, mcq, short = setup
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 3)
    _add_items(s, mcq.chunk_id, 5, "new")  # 7 new items in all
    assert len(due_items(s, user.id)) == 3 and due_count(s, user.id) == 3
    submit_answer(s, user.id, mcq.id, "1")
    submit_answer(s, user.id, short.id, "x", FakeLLM(1.0))
    assert due_count(s, user.id) == 1
    # a lapsed review is still due; it isn't new, so the cap doesn't hide it
    _make_due(s, user, mcq)
    assert [i.id for i in due_items(s, user.id)][0] == mcq.id
    assert due_count(s, user.id) == 2
    # yesterday's first answers don't count against today
    for state in s.exec(select(ReviewState)).all():
        state.first_answered_at -= timedelta(days=1)
        s.add(state)
    s.commit()
    assert due_count(s, user.id) == 4  # mcq + 3 new


@pytest.mark.parametrize("same_item", [True, False])
def test_concurrent_new_item_rechecked_under_user_lock(setup, monkeypatch, same_item):
    s, user, mcq, short = setup
    if not same_item:  # same item: the post-lock state re-read rejects it, cap or not
        monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 1)
    llm = ReplyLLM({"correct": True, "partial_credit": 1.0})
    original = grade._lock_user

    def racing(session, user_id):
        # another request took the last slot while this one waited on the lock
        monkeypatch.setattr(grade, "_lock_user", original)
        with Session(s.get_bind()) as other:
            if same_item:
                submit_answer(other, user.id, short.id, "because", llm)
            else:
                submit_answer(other, user.id, mcq.id, "1")
        original(session, user_id)

    monkeypatch.setattr(grade, "_lock_user", racing)
    with pytest.raises(NotDue):
        submit_answer(s, user.id, short.id, "because", llm)
    assert llm.calls == (1 if same_item else 0)  # the loser never graded
    assert len(s.exec(select(ReviewState)).all()) == 1


def test_review_of_pre_cap_row_spends_no_slot(setup, monkeypatch):
    s, user, mcq, short = setup
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 1)
    submit_answer(s, user.id, mcq.id, "1")
    # a row from before migration 0008 has no first_answered_at
    state = s.exec(select(ReviewState)).one()
    state.first_answered_at = None
    s.add(state)
    s.commit()
    _make_due(s, user, mcq)
    submit_answer(s, user.id, mcq.id, "1")
    s.refresh(state)
    assert state.first_answered_at is None
    assert short.id in [i.id for i in due_items(s, user.id)]  # today's slot still free


def test_new_item_past_daily_cap_rejected(setup, monkeypatch):
    s, user, mcq, short = setup
    monkeypatch.setattr(grade, "NEW_ITEMS_PER_DAY", 1)
    llm = ReplyLLM({"correct": True, "partial_credit": 1.0})
    submit_answer(s, user.id, mcq.id, "1")
    # short is owned but outside today's queue: no grading, nothing recorded
    assert [i.id for i in due_items(s, user.id)] == []
    with pytest.raises(NotDue):
        submit_answer(s, user.id, short.id, "because", llm)
    assert llm.calls == 0
    assert len(s.exec(select(ReviewState)).all()) == 1
    # a due review of an already-started item is still accepted
    _make_due(s, user, mcq)
    assert submit_answer(s, user.id, mcq.id, "1")["correct"] is True


def test_nul_in_answer_is_stripped(setup):
    # Postgres rejects NUL in last_answer: a pasted one must not 500
    s, user, _, short = setup
    llm = ReplyLLM({"correct": True, "partial_credit": 1.0})
    submit_answer(s, user.id, short.id, "be\x00cause", llm)
    state = s.exec(select(ReviewState).where(ReviewState.quiz_item_id == short.id)).one()
    assert state.last_answer == "because"
