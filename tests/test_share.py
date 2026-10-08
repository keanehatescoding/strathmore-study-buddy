"""Cross-user reuse of extraction, chunks and quiz items (app.share)."""

import pytest
from sqlmodel import Session, SQLModel, select

from app.models import Chunk, Course, QuizAttempt, QuizItem, Resource, Topic, User
from app.pipeline import run_chunking, run_extraction, run_quiz
from app.share import copy_quiz
from tests.dbutil import make_engine

TEXT = " ".join(f"Tree fact {i}." for i in range(60))  # needs the chunker
ITEMS = [
    {"question": "What is a tree?", "question_type": "short_answer",
     "correct_answer": "hierarchy", "grading_criteria": "mentions hierarchy",
     "explanation": "slides", "difficulty": "recall"},
    {"question": "Which is a tree?", "question_type": "mcq",
     "options": ["array", "tree", "queue", "stack"], "correct_answer": 1,
     "explanation": "Trees branch.", "difficulty": "application"},
]


class ChunkLLM:
    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, **kw):
        self.calls += 1
        text = user.split("\n\n", 1)[1]
        half = len(text) // 2
        return {"chunks": [{"title": "C0", "content": text[:half]},
                           {"title": "C1", "content": text[half:]}]}


class QuizLLM:
    def __init__(self, items=ITEMS):
        self.items, self.calls = items, 0

    def complete_json(self, system, user, **kw):
        self.calls += 1
        return {"items": self.items}


class NoLLM:
    def complete_json(self, *a, **kw):
        raise AssertionError("LLM called despite a donor")


def no_download(r):
    raise AssertionError("downloaded despite a donor")


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _copy_of(session, email, **kw):
    """One user's synced copy of a course with one resource."""
    user = User(email=email)
    session.add(user)
    session.commit()
    course = Course(user_id=user.id, source="moodle", source_id="c1", name="C")
    session.add(course)
    session.commit()
    topic = Topic(course_id=course.id, source_id="t1", title="T")
    session.add(topic)
    session.commit()
    kw.setdefault("source", "moodle")
    kw.setdefault("type", "file")
    kw.setdefault("content_hash", "fp:same")
    kw.setdefault("status", "pending")
    r = Resource(topic_id=topic.id, source_id="r1", title="R", **kw)
    session.add(r)
    session.commit()
    session.refresh(r)
    return course, r


def _chunks(session, r):
    return session.exec(select(Chunk).where(Chunk.resource_id == r.id)
                        .order_by(Chunk.order)).all()


def _items(session, r):
    return session.exec(select(QuizItem).join(Chunk).where(Chunk.resource_id == r.id)
                        .order_by(QuizItem.generation_key)).all()


def test_extraction_copies_a_donors_text(session):
    _copy_of(session, "a@x", status="extracted", extracted_text=TEXT)
    course, r = _copy_of(session, "b@x", attempts=2)

    res = run_extraction(session, no_download, course.id)

    session.refresh(r)
    assert res.counts["shared"] == 1
    assert (r.status, r.extracted_text, r.attempts) == ("extracted", TEXT, 0)


@pytest.mark.parametrize("donor", [
    {"content_hash": "fp:other"}, {"source": "classroom"}, {"type": "link"},
    {"status": "failed"}, {"extracted_text": "   "},
])
def test_extraction_ignores_unlike_donors(session, donor):
    kw = {"status": "extracted", "extracted_text": TEXT, **donor}
    _copy_of(session, "a@x", **kw)
    course, r = _copy_of(session, "b@x")

    run_extraction(session, lambda r: (b"own text", "text/plain"), course.id)

    session.refresh(r)
    assert (r.status, r.extracted_text) == ("extracted", "own text")


def test_extraction_without_a_token_stays_pending(session):
    # sharing must not hand material to an owner whose access has lapsed
    _copy_of(session, "a@x", status="extracted", extracted_text=TEXT)
    course, r = _copy_of(session, "b@x")

    res = run_extraction(session, None, course.id, downloader_for=lambda r: None)

    session.refresh(r)
    assert res.counts["no_token"] == 1 and r.status == "pending"


def test_chunks_and_quiz_are_paid_for_once(session):
    course_a, a = _copy_of(session, "a@x", status="extracted", extracted_text=TEXT)
    course_b, b = _copy_of(session, "b@x", status="extracted", extracted_text=TEXT)
    chunk_llm, quiz_llm = ChunkLLM(), QuizLLM()

    run_chunking(session, chunk_llm, course_a.id)
    res = run_chunking(session, NoLLM(), course_b.id)
    assert chunk_llm.calls == 1 and res.counts["shared"] == 1
    assert ([(c.title, c.content, c.start_char, c.end_char) for c in _chunks(session, b)]
            == [(c.title, c.content, c.start_char, c.end_char) for c in _chunks(session, a)])
    assert {c.id for c in _chunks(session, b)}.isdisjoint(c.id for c in _chunks(session, a))

    run_quiz(session, quiz_llm, course_a.id)
    res = run_quiz(session, NoLLM(), course_b.id)
    assert quiz_llm.calls == 2 and res.counts["shared"] == 2 and res.counts["items"] == 4
    own_items = _items(session, b)
    assert [i.question for i in own_items] == [i.question for i in _items(session, a)]
    for item in own_items:  # keys are the copy's own, so idempotency holds
        assert item.generation_key.split(":")[0] == str(item.chunk_id)
    assert all(session.get(QuizAttempt, (c.id, 1)) for c in _chunks(session, b))
    assert run_quiz(session, NoLLM(), course_b.id).counts["skipped"] == 2


def test_chunks_need_identical_text(session):
    _copy_of(session, "a@x", status="extracted", extracted_text=TEXT)
    course_a = session.exec(select(Course)).first()
    run_chunking(session, ChunkLLM(), course_a.id)
    course_b, b = _copy_of(session, "b@x", status="extracted", extracted_text=TEXT + "!")
    llm = ChunkLLM()

    run_chunking(session, llm, course_b.id)

    assert llm.calls == 1 and _chunks(session, b)[-1].content.endswith("!")


def test_chunk_copy_replaces_stale_chunks(session):
    course_a, a = _copy_of(session, "a@x", status="extracted", extracted_text=TEXT)
    run_chunking(session, ChunkLLM(), course_a.id)
    course_b, b = _copy_of(session, "b@x", status="pending", extracted_text=TEXT)
    session.add(Chunk(resource_id=b.id, title="old", content="old", order=0))
    session.commit()

    run_chunking(session, NoLLM(), course_b.id)

    assert [c.title for c in _chunks(session, b)] == ["C0", "C1"]


def test_empty_quiz_attempt_is_shared_too(session):
    course_a, a = _copy_of(session, "a@x", status="extracted", extracted_text="hi")
    course_b, b = _copy_of(session, "b@x", status="extracted", extracted_text="hi")
    run_chunking(session, None, course_a.id)
    run_chunking(session, None, course_b.id)
    run_quiz(session, QuizLLM(items=[]), course_a.id)

    res = run_quiz(session, NoLLM(), course_b.id)

    assert res.counts["shared"] == 1 and _items(session, b) == []


def test_quiz_copies_only_the_requested_attempt(session):
    course_a, a = _copy_of(session, "a@x", status="extracted", extracted_text="hi")
    course_b, b = _copy_of(session, "b@x", status="extracted", extracted_text="hi")
    run_chunking(session, None, course_a.id)
    run_chunking(session, None, course_b.id)
    run_quiz(session, QuizLLM(), course_a.id, attempt=1)
    run_quiz(session, QuizLLM(), course_a.id, attempt=2)
    own = _chunks(session, b)[0]

    assert len(copy_quiz(session, own, attempt=2)) == 2
    assert copy_quiz(session, own, attempt=3) is None
    assert {i.generation_key for i in _items(session, b)} == {
        f"{own.id}:2:0", f"{own.id}:2:1"}


def test_extraction_looks_past_blank_donors(session):
    for i in range(3):  # random ids: the good donor's position varies
        _copy_of(session, f"blank{i}@x", status="extracted", extracted_text=" \n")
    _copy_of(session, "a@x", status="extracted", extracted_text=TEXT)
    course, r = _copy_of(session, "b@x")

    run_extraction(session, no_download, course.id)

    session.refresh(r)
    assert r.extracted_text == TEXT


def test_quiz_needs_the_same_resource_type(session):
    _, a = _copy_of(session, "a@x", type="link", status="extracted", extracted_text=TEXT)
    _, b = _copy_of(session, "b@x", status="extracted", extracted_text=TEXT)
    donor = Chunk(resource_id=a.id, title="C", content="same", order=0)
    own = Chunk(resource_id=b.id, title="C", content="same", order=0)
    session.add_all([donor, own])
    session.commit()
    session.add(QuizAttempt(chunk_id=donor.id, attempt=1))
    session.commit()

    assert copy_quiz(session, own) is None


class Drive:
    """A user's own Drive downloader: whether their grant sees the file."""

    def __init__(self, readable):
        self.readable, self.asked = readable, []

    def can_read(self, url):
        self.asked.append(url)
        return self.readable

    def __call__(self, url):
        return b"own text", "text/plain"


def _classroom_pair(session):
    kw = {"source": "classroom", "content_hash": "drive:1AbCdEfGhIjKlMnOp",
          "raw_url": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view"}
    _copy_of(session, "a@x", status="extracted", extracted_text=TEXT, **kw)
    return _copy_of(session, "b@x", **kw)


@pytest.mark.parametrize("readable", [True, False])
def test_classroom_files_are_shared_only_if_the_users_grant_sees_them(session, readable):
    course, r = _classroom_pair(session)
    drive = Drive(readable)

    res = run_extraction(session, None, course.id, downloader_for=lambda r: drive)

    session.refresh(r)
    assert drive.asked == [r.raw_url]
    assert res.counts["shared"] == int(readable)
    assert r.extracted_text == (TEXT if readable else "own text")


def test_classroom_files_need_a_downloader_that_can_check(session):
    course, r = _classroom_pair(session)

    run_extraction(session, lambda url: (b"own text", "text/plain"), course.id)

    session.refresh(r)
    assert r.extracted_text == "own text"


def test_classroom_check_is_skipped_without_a_donor(session):
    kw = {"source": "classroom", "content_hash": "drive:other"}
    course, r = _copy_of(session, "b@x", **kw)
    drive = Drive(True)

    run_extraction(session, None, course.id, downloader_for=lambda r: drive)

    assert drive.asked == []


def _broken(*a, **kw):
    raise RuntimeError("db hiccup")


def test_a_failed_extraction_share_falls_back_to_downloading(session, monkeypatch):
    import app.pipeline

    monkeypatch.setattr(app.pipeline, "copy_extraction", _broken)
    _copy_of(session, "a@x", status="extracted", extracted_text=TEXT)
    course, r = _copy_of(session, "b@x")
    _, other = _copy_of(session, "c@x", content_hash="fp:other")

    res = run_extraction(session, lambda url: (b"own text", "text/plain"))

    session.refresh(r)
    session.refresh(other)
    assert res.counts["share_errors"] == 2 and res.counts["extracted"] == 2
    assert r.extracted_text == other.extracted_text == "own text"


def test_a_failed_chunk_share_falls_back_to_chunking(session, monkeypatch):
    import app.pipeline

    monkeypatch.setattr(app.pipeline, "copy_chunks", _broken)
    course, r = _copy_of(session, "b@x", status="extracted", extracted_text=TEXT)
    llm = ChunkLLM()

    res = run_chunking(session, llm, course.id)

    assert res.counts["share_errors"] == 1 and llm.calls == 1
    assert len(_chunks(session, r)) == 2


def test_a_shared_copy_keeps_the_donors_cap_note(session):
    note = "Only the first 10 of 20 characters are studied: the rest is past the size cap"
    _copy_of(session, "a@x", status="extracted", extracted_text=TEXT, error=note)
    course, r = _copy_of(session, "b@x")

    run_extraction(session, no_download, course.id)

    session.refresh(r)
    assert (r.status, r.error) == ("extracted", note)


def test_a_shared_copy_of_an_uncapped_donor_is_capped(session):
    from app.extract import MAX_EXTRACTED_CHARS, is_cap_note

    big = "A line of a very long book.\n" * (MAX_EXTRACTED_CHARS // 10)
    # extracted before the cap existed
    _copy_of(session, "a@x", status="extracted", extracted_text=big)
    course, r = _copy_of(session, "b@x")

    run_extraction(session, no_download, course.id)

    session.refresh(r)
    assert len(r.extracted_text) <= MAX_EXTRACTED_CHARS
    assert is_cap_note(r.error) and f"{len(big):,}" in r.error
