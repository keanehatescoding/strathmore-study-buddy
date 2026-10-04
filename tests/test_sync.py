"""Phase 1 tests: diff-based sync via a FakeAdapter (no network)."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from app.models import Assignment, Chunk, Course, QuizItem, Resource, ReviewState, Topic, User
from app.sync import (
    AssignmentData,
    CourseData,
    ResourceData,
    TopicData,
    content_hash,
    link_type,
    sync_all,
    sync_course,
)
from tests.dbutil import make_engine


class FakeAdapter:
    source = "moodle"

    def __init__(self):
        self.courses = [CourseData("c1", "CS 301", "CS 301")]
        self.topics = {"c1": [TopicData("t1", "Trees", 0)]}
        self.resources = {
            ("c1", "t1"): [
                ResourceData("t1", "r-file", "file", "trees.pdf",
                             raw_url="https://x/f.pdf", content_bytes=b"v1"),
                ResourceData("t1", "r-page", "page_text", "Overview",
                             text="A tree is..."),
                ResourceData("t1", "r-link", "link", "Docs",
                             raw_url="https://example.com",
                             content_bytes=b"https://example.com"),
            ]
        }
        self.assignments = {
            "c1": [AssignmentData("a1", "Assignment 1", "t1",
                                  datetime(2026, 10, 1, tzinfo=timezone.utc), "Do trees")]
        }

    def fetch_courses(self):
        self.course_fetches = getattr(self, "course_fetches", 0) + 1
        return self.courses
    def fetch_topics(self, cid): return self.topics[cid]
    def fetch_resources(self, cid, tid): return self.resources[(cid, tid)]
    def fetch_assignments(self, cid): return self.assignments[cid]


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def user_id(session):
    user = User(email="s@x.edu")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.id


def test_first_sync_inserts(session, user_id):
    stats = sync_course(session, FakeAdapter(), "c1", user_id)
    assert (stats.courses_new, stats.topics_new) == (1, 1)
    assert stats.resources_new == 3
    assert stats.assignments_new == 1
    assert session.exec(select(Resource)).all().__len__() == 3
    page = session.exec(select(Resource).where(Resource.source_id == "r-page")).one()
    assert page.status == "extracted" and page.extracted_text == "A tree is..."
    asg = session.exec(select(Assignment)).one()
    assert asg.title == "Assignment 1"  # deadlines never land in resources


def test_second_sync_skips_everything(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    stats = sync_course(session, adapter, "c1", user_id)
    assert stats.resources_new == 0 and stats.resources_updated == 0
    assert stats.resources_skipped == 3


def test_changed_content_requeues(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    adapter.resources[("c1", "t1")][0] = ResourceData(
        "t1", "r-file", "file", "trees.pdf",
        raw_url="https://x/f.pdf",
        content_bytes="v2 — lecturer updated slides".encode("utf-8"))
    stats = sync_course(session, adapter, "c1", user_id)
    assert (stats.resources_updated, stats.resources_skipped) == (1, 2)
    updated = session.exec(select(Resource).where(Resource.source_id == "r-file")).one()
    assert updated.status == "pending" and updated.extracted_text is None


def test_unknown_course_raises(session, user_id):
    with pytest.raises(ValueError):
        sync_course(session, FakeAdapter(), "nope", user_id)


def test_same_source_course_per_user(session, user_id):
    other = User(email="other@x.edu")
    session.add(other)
    session.commit()
    session.refresh(other)
    sync_course(session, FakeAdapter(), "c1", user_id)
    stats = sync_course(session, FakeAdapter(), "c1", other.id)
    assert stats.courses_new == 1  # no unique-clash across users
    mine = session.exec(
        select(Course).where(Course.user_id == user_id)).all()
    assert len(mine) == 1


def test_unowned_sync_reuses_unowned_course(session):
    sync_course(session, FakeAdapter(), "c1", None)
    stats = sync_course(session, FakeAdapter(), "c1", None)
    assert stats.courses_new == 0
    assert len(session.exec(select(Course)).all()) == 1


def test_user_sync_adopts_pre_auth_course(session, user_id):
    sync_course(session, FakeAdapter(), "c1", None)
    stats = sync_course(session, FakeAdapter(), "c1", user_id)
    assert stats.courses_new == 0 and stats.resources_new == 0
    course = session.exec(select(Course)).one()
    assert course.user_id == user_id


def test_adoption_loses_race_without_overwriting(session, user_id):
    from sqlmodel import update

    from app.sync import _adopt_unowned

    other = User(email="other@x.edu")
    session.add(other)
    session.add(Course(source="moodle", source_id="c1", name="CS 301"))
    session.commit()
    course = session.exec(select(Course)).one()
    stale = select(Course).where(Course.id == course.id)  # loaded while unowned
    # another worker claims it between our SELECT and UPDATE
    session.exec(update(Course).values(user_id=other.id))
    session.commit()
    assert _adopt_unowned(session, stale, user_id) is None
    session.refresh(course)
    assert course.user_id == other.id


def test_lost_claim_to_same_user_reuses_their_course(session, user_id, monkeypatch):
    import app.sync as sync_mod

    session.add(Course(source="moodle", source_id="c1", name="CS 301"))
    session.commit()

    def lose_to_self(session, unowned, uid):
        # a concurrent sync for the same user claims the row first
        session.exec(sync_mod.update(Course).values(user_id=uid))
        session.commit()
        return None

    monkeypatch.setattr(sync_mod, "_adopt_unowned", lose_to_self)
    stats = sync_course(session, FakeAdapter(), "c1", user_id)
    assert stats.courses_new == 0
    assert session.exec(select(Course)).one().user_id == user_id


def test_link_type():
    assert link_type("https://www.youtube.com/watch?v=abc") == "video"
    assert link_type("https://youtu.be/abc") == "video"
    assert link_type("https://example.com/notes") == "link"
    assert content_hash(ResourceData("t", "s", "link", "L")) is None


def _derive(session, resource, user_id, tag):
    chunk = Chunk(resource_id=resource.id, title=tag, content=tag)
    session.add(chunk)
    session.commit()
    item = QuizItem(chunk_id=chunk.id, question="q", question_type="mcq",
                    correct_answer="0", generation_key=f"{tag}:1:0")
    session.add(item)
    session.commit()
    session.add(ReviewState(user_id=user_id, quiz_item_id=item.id,
                            next_review_date=datetime.now(timezone.utc)))
    session.commit()


def _resource(session, source_id):
    return session.exec(select(Resource).where(Resource.source_id == source_id)).one()


def test_changed_content_purges_derived_rows(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    _derive(session, _resource(session, "r-file"), user_id, "file")
    _derive(session, _resource(session, "r-page"), user_id, "page")
    adapter.resources[("c1", "t1")][0].content_bytes = b"v2"
    sync_course(session, adapter, "c1", user_id)
    session.expire_all()
    assert [c.title for c in session.exec(select(Chunk)).all()] == ["page"]
    assert len(session.exec(select(QuizItem)).all()) == 1
    assert len(session.exec(select(ReviewState)).all()) == 1


def test_title_only_change_updates_without_reset(session, user_id):
    adapter = FakeAdapter()
    adapter.resources[("c1", "t1")].append(
        ResourceData("t1", "r-nohash", "link", "Old", raw_url="https://a"))
    sync_course(session, adapter, "c1", user_id)
    _derive(session, _resource(session, "r-page"), user_id, "page")
    res = adapter.resources[("c1", "t1")]
    res[1].title = "Overview (revised)"
    res[3].title, res[3].raw_url = "New", "https://b"
    stats = sync_course(session, adapter, "c1", user_id)
    assert (stats.resources_updated, stats.resources_skipped) == (2, 2)
    session.expire_all()
    page = _resource(session, "r-page")
    assert page.title == "Overview (revised)"
    assert page.status == "extracted" and page.extracted_text == "A tree is..."
    assert len(session.exec(select(Chunk)).all()) == 1  # not purged
    nohash = _resource(session, "r-nohash")
    assert (nohash.title, nohash.raw_url) == ("New", "https://b")


def test_unchanged_assignment_not_counted(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    assert sync_course(session, adapter, "c1", user_id).assignments_updated == 0
    adapter.assignments["c1"][0].title = "Assignment 1 (extended)"
    assert sync_course(session, adapter, "c1", user_id).assignments_updated == 1


def _to_fingerprint(session, user_id, fetch_content):
    """Sync with a legacy content hash, then switch the file to a fingerprint."""
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)  # legacy full-content hash (b"v1")
    _derive(session, _resource(session, "r-file"), user_id, "file")
    f = adapter.resources[("c1", "t1")][0]
    f.content_bytes, f.fingerprint = None, "https://x/content/1/f.pdf|100|1700000000"
    adapter.fetch_content = fetch_content
    return adapter, f


def test_legacy_hash_verified_same_bytes_keeps_progress(session, user_id):
    adapter, f = _to_fingerprint(session, user_id, lambda r: b"v1")
    stats = sync_course(session, adapter, "c1", user_id)
    assert stats.resources_updated == 0
    assert len(session.exec(select(Chunk)).all()) == 1
    assert _resource(session, "r-file").content_hash.startswith("fp:")
    f.fingerprint = "https://x/content/2/f.pdf|100|1700000000"  # revision bump
    assert sync_course(session, adapter, "c1", user_id).resources_updated == 1
    session.expire_all()
    assert _resource(session, "r-file").status == "pending"
    assert session.exec(select(Chunk)).all() == []


def test_legacy_hash_verified_changed_bytes_resets(session, user_id):
    adapter, _ = _to_fingerprint(session, user_id, lambda r: b"v2 new slides")
    assert sync_course(session, adapter, "c1", user_id).resources_updated == 1
    session.expire_all()
    res = _resource(session, "r-file")
    assert res.status == "pending" and res.content_hash.startswith("fp:")
    assert session.exec(select(Chunk)).all() == []


def test_legacy_hash_unverifiable_keeps_legacy_and_retries(session, user_id):
    def down(r):
        raise RuntimeError("moodle down")

    adapter, _ = _to_fingerprint(session, user_id, down)
    legacy = _resource(session, "r-file").content_hash
    assert sync_course(session, adapter, "c1", user_id).resources_updated == 0
    assert _resource(session, "r-file").content_hash == legacy
    assert len(session.exec(select(Chunk)).all()) == 1
    adapter.fetch_content = lambda r: b"v1"  # next sync can verify
    sync_course(session, adapter, "c1", user_id)
    assert _resource(session, "r-file").content_hash.startswith("fp:")


def test_sync_all_fetches_courses_once(session, user_id):
    adapter = FakeAdapter()
    adapter.courses.append(CourseData("c2", "CS 302"))
    adapter.topics["c2"] = []
    adapter.assignments["c2"] = []
    assert set(sync_all(session, adapter, user_id)) == {"c1", "c2"}
    assert adapter.course_fetches == 1


class FakeMoodleClient:
    def __init__(self):
        self.calls: list[str] = []

    def site_info(self):
        return {"userid": 7}

    def get_users_courses(self, userid):
        return [{"id": 5, "fullname": "CS 301", "shortname": "CS301"}]

    def get_course_contents(self, courseid):
        self.calls.append("contents")
        file = {"type": "file", "filename": "a.pdf", "filepath": "/",
                "fileurl": "https://m/a.pdf", "filesize": 10, "timemodified": 1,
                "mimetype": "application/pdf"}
        return [
            {"id": s, "name": f"S{s}", "modules": [
                {"id": s * 10, "modname": "resource", "name": "A", "contents": [file]},
                {"id": s * 10 + 1, "modname": "page", "name": "P"},
                {"id": s * 10 + 2, "modname": "page", "name": "Q"},
            ]}
            for s in (1, 2)
        ]

    def get_assignments(self, courseid):
        return {"courses": []}

    def call(self, function, **params):
        self.calls.append(function)
        return {"pages": [{"coursemodule": 11, "content": "page 11"}]}

    def download(self, fileurl):
        raise AssertionError("sync must not download files")


def test_moodle_adapter_fetches_contents_once_and_never_downloads(session, user_id):
    from app.moodle import MoodleAdapter

    client = FakeMoodleClient()
    stats = sync_all(session, MoodleAdapter(client), user_id)["5"]
    assert stats.resources_new == 6
    assert client.calls == ["contents", "mod_page_get_pages_by_courses"]
    f = _resource(session, "10")
    assert f.content_hash.startswith("fp:") and f.mime_type == "application/pdf"
    assert _resource(session, "11").extracted_text == "page 11"


def test_resource_moved_between_topics_keeps_row_and_progress(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    original = _resource(session, "r-page")
    _derive(session, original, user_id, "page")
    adapter.topics["c1"].append(TopicData("t2", "Graphs", 1))
    page = adapter.resources[("c1", "t1")].pop(1)
    adapter.resources[("c1", "t2")] = [page]
    stats = sync_course(session, adapter, "c1", user_id)
    assert (stats.resources_new, stats.resources_updated, stats.resources_removed) == (0, 1, 0)
    session.expire_all()
    moved = _resource(session, "r-page")  # .one(): no copy left in t1
    assert moved.id == original.id
    assert session.get(Topic, moved.topic_id).source_id == "t2"
    assert moved.status == "extracted"
    assert len(session.exec(select(ReviewState)).all()) == 1  # progress kept
    assert sync_course(session, adapter, "c1", user_id).resources_skipped == 3


def test_duplicate_left_by_earlier_move_is_retired(session, user_id):
    adapter = FakeAdapter()
    adapter.topics["c1"].append(TopicData("t2", "Graphs", 1))
    adapter.resources[("c1", "t2")] = []
    sync_course(session, adapter, "c1", user_id)
    # what the old per-topic lookup produced: the same material in both topics
    old = _resource(session, "r-page")
    t2 = session.exec(select(Topic).where(Topic.source_id == "t2")).one()
    copy = Resource(topic_id=t2.id, source="moodle", source_id="r-page", type="page_text",
                    title="Overview", extracted_text="A tree is...",
                    content_hash=old.content_hash, status="extracted")
    session.add(copy)
    session.commit()
    _derive(session, copy, user_id, "copy")
    adapter.resources[("c1", "t2")] = [adapter.resources[("c1", "t1")].pop(1)]

    stats = sync_course(session, adapter, "c1", user_id)
    assert stats.resources_removed == 1
    session.expire_all()
    kept = _resource(session, "r-page")
    assert kept.topic_id == t2.id  # the copy with progress wins
    assert [c.title for c in session.exec(select(Chunk)).all()] == ["copy"]


def test_resource_missing_from_source_is_left_alone(session, user_id):
    adapter = FakeAdapter()
    sync_course(session, adapter, "c1", user_id)
    adapter.resources[("c1", "t1")].pop(1)
    stats = sync_course(session, adapter, "c1", user_id)
    assert stats.resources_removed == 0
    assert len(session.exec(select(Resource)).all()) == 3


def test_old_copy_with_progress_wins_over_empty_destination_copy(session, user_id):
    adapter = FakeAdapter()
    adapter.topics["c1"].append(TopicData("t2", "Graphs", 1))
    adapter.resources[("c1", "t2")] = []
    sync_course(session, adapter, "c1", user_id)
    old = _resource(session, "r-page")
    _derive(session, old, user_id, "old")
    t2 = session.exec(select(Topic).where(Topic.source_id == "t2")).one()
    session.add(Resource(topic_id=t2.id, source="moodle", source_id="r-page",
                         type="page_text", title="Overview", extracted_text="A tree is...",
                         content_hash=old.content_hash, status="extracted"))
    session.commit()
    adapter.resources[("c1", "t2")] = [adapter.resources[("c1", "t1")].pop(1)]

    stats = sync_course(session, adapter, "c1", user_id)
    assert stats.resources_removed == 1
    session.expire_all()
    kept = _resource(session, "r-page")
    assert kept.id == old.id and kept.topic_id == t2.id  # moved into the destination
    assert [c.title for c in session.exec(select(Chunk)).all()] == ["old"]
    assert len(session.exec(select(ReviewState)).all()) == 1


class _PagesDown(FakeMoodleClient):
    """Course contents load, but the page API fails (timeout, 5xx, ...)."""

    def call(self, function, **params):
        from app.moodle import MoodleError

        self.calls.append(function)
        raise MoodleError(f"{function} request failed: timed out")


class _PageMissing(FakeMoodleClient):
    def call(self, function, **params):
        self.calls.append(function)
        return {"pages": []}


def _progress(session):
    return tuple(len(session.exec(select(m)).all()) for m in (Chunk, QuizItem, ReviewState))


@pytest.mark.parametrize("broken", [_PagesDown, _PageMissing])
def test_page_fetch_failure_keeps_content_and_progress(session, user_id, broken):
    from app.moodle import MoodleAdapter

    sync_all(session, MoodleAdapter(FakeMoodleClient()), user_id)
    page = _resource(session, "11")
    page.status = "chunked"
    session.add(page)
    session.commit()
    _derive(session, page, user_id, "page")
    digest = page.content_hash
    stats = sync_all(session, MoodleAdapter(broken()), user_id)["5"]
    assert _progress(session) == (1, 1, 1)
    page = _resource(session, "11")
    assert (page.extracted_text, page.status, page.content_hash) == ("page 11", "chunked", digest)
    assert stats.resources_updated == 0
    # a real edit afterwards still resets the page
    client = FakeMoodleClient()
    client.call = lambda function, **p: {"pages": [{"coursemodule": 11, "content": "v2"}]}
    sync_all(session, MoodleAdapter(client), user_id)
    assert _progress(session) == (0, 0, 0)
    assert _resource(session, "11").extracted_text == "v2"


def test_page_first_seen_during_outage_is_filled_in_later(session, user_id):
    from app.moodle import MoodleAdapter

    sync_all(session, MoodleAdapter(_PagesDown()), user_id)
    page = _resource(session, "11")
    assert (page.extracted_text, page.content_hash, page.status) == (None, None, "pending")
    sync_all(session, MoodleAdapter(FakeMoodleClient()), user_id)
    page = _resource(session, "11")
    assert (page.extracted_text, page.status) == ("page 11", "extracted")
    assert page.content_hash is not None


# -- issue #28: isolation, size caps, page HTML, YouTube forms ------------------


class _BrokenCourse(FakeAdapter):
    """c1 fails mid-sync (e.g. MoodleError / HttpError); c2 is fine."""

    def __init__(self):
        super().__init__()
        self.courses.append(CourseData("c2", "CS 302"))
        self.topics["c2"] = [TopicData("t2", "Graphs", 0)]
        self.resources[("c2", "t2")] = [
            ResourceData("t2", "g", "page_text", "Graphs", text="A graph is...")]
        self.assignments["c2"] = []

    def fetch_topics(self, cid):
        if cid == "c1":
            from app.moodle import MoodleError

            raise MoodleError("core_course_get_contents: accessexception")
        return super().fetch_topics(cid)


def test_one_failing_course_does_not_abort_the_rest(session, user_id):
    results = sync_all(session, _BrokenCourse(), user_id)
    assert results["c1"].error == (
        "MoodleError: core_course_get_contents: accessexception")
    assert results["c1"].as_dict()["error"] == results["c1"].error
    assert results["c2"].error is None and "error" not in results["c2"].as_dict()
    assert results["c2"].resources_new == 1
    assert _resource(session, "g").extracted_text == "A graph is..."


def _sync_job(session, monkeypatch, adapter):
    import app.sync_cli as sync_cli
    from app.jobs import run_sync_job

    monkeypatch.setattr(sync_cli, "build_adapter", lambda source, user: adapter)
    return run_sync_job(session, {"source": "moodle", "course_id": None,
                                  "user_email": "s@x.edu"})


def test_sync_job_completes_with_per_course_errors(session, user_id, monkeypatch):
    result = _sync_job(session, monkeypatch, _BrokenCourse())
    assert "accessexception" in result["c1"]["error"]
    assert result["c2"]["resources_new"] == 1


def test_sync_job_fails_when_every_course_fails(session, user_id, monkeypatch):
    adapter = _BrokenCourse()
    adapter.courses = adapter.courses[:1]
    with pytest.raises(RuntimeError, match="every course failed"):
        _sync_job(session, monkeypatch, adapter)


def test_all_users_sync_continues_past_a_failing_user(session, monkeypatch, capsys):
    import app.sync_cli as sync_cli

    for email in ("a@x.edu", "b@x.edu", "c@x.edu"):
        session.add(User(email=email))
    session.commit()
    engine = session.get_bind()
    synced = []

    def fake_inline(s, source, user, course):
        if user.email == "b@x.edu":
            raise RuntimeError("invalid_grant")
        synced.append(user.email)

    monkeypatch.setattr(sync_cli, "engine", engine)
    monkeypatch.setattr(sync_cli, "has_credentials", lambda source, u: True)
    monkeypatch.setattr(sync_cli, "_sync_inline", fake_inline)
    monkeypatch.setattr("sys.argv", ["sync_cli", "--source", "moodle", "--all-users"])
    with pytest.raises(SystemExit, match="1 of 3 users"):
        sync_cli.main()
    assert synced == ["a@x.edu", "c@x.edu"]
    assert "b@x.edu: sync failed: RuntimeError: invalid_grant" in capsys.readouterr().err


def test_oversized_file_is_skipped_without_download(session, user_id):
    from app.extract import MAX_DOWNLOAD_BYTES

    adapter = FakeAdapter()
    big = ResourceData("t1", "big", "file", "lecture.mp4", raw_url="https://x/big",
                       fingerprint="big|1", size=MAX_DOWNLOAD_BYTES + 1)
    adapter.resources[("c1", "t1")].append(big)
    sync_course(session, adapter, "c1", user_id)
    row = _resource(session, "big")
    assert row.status == "skipped" and "over 50 MB" in row.error
    # re-uploaded smaller: queued for extraction again
    big.fingerprint, big.size = "big|2", 1000
    sync_course(session, adapter, "c1", user_id)
    row = _resource(session, "big")
    assert (row.status, row.error) == ("pending", None)


def test_moodle_page_html_is_stored_as_text(session, user_id):
    from app.moodle import MoodleAdapter

    client = FakeMoodleClient()
    client.call = lambda f, **p: {"pages": [
        {"coursemodule": 11, "content": "<p>Trees &amp; <b>graphs</b></p>"}]}
    sync_all(session, MoodleAdapter(client), user_id)
    assert _resource(session, "11").extracted_text == "Trees & graphs"


def test_page_stored_as_raw_html_gets_text_and_keeps_progress(session, user_id):
    """Rows synced before HTML conversion: same hash, so no reset."""
    from app.moodle import MoodleAdapter

    html = "<p>Trees &amp; <b>graphs</b></p>"
    client = FakeMoodleClient()
    client.call = lambda f, **p: {"pages": [{"coursemodule": 11, "content": html}]}
    sync_all(session, MoodleAdapter(client), user_id)
    page = _resource(session, "11")
    page.extracted_text = html  # as the old code stored it
    page.status = "chunked"
    session.add(page)
    session.commit()
    _derive(session, page, user_id, "page")
    stats = sync_all(session, MoodleAdapter(client), user_id)["5"]
    page = _resource(session, "11")
    assert (page.extracted_text, page.status) == ("Trees & graphs", "chunked")
    assert _progress(session) == (1, 1, 1)
    assert stats.resources_updated == 1



def test_better_text_relocates_kept_chunks(session, user_id):
    """Chunk offsets index extracted_text, so a text refresh re-finds them."""
    from app.moodle import MoodleAdapter

    html = "<p>Trees &amp; <b>graphs</b></p>"
    client = FakeMoodleClient()
    client.call = lambda f, **p: {"pages": [{"coursemodule": 11, "content": html}]}
    sync_all(session, MoodleAdapter(client), user_id)
    page = _resource(session, "11")
    page.extracted_text = html  # as the old code stored it
    page.status = "chunked"
    session.add_all([
        page,
        Chunk(resource_id=page.id, title="a", content="graphs", order=0,
              start_char=html.find("graphs"), end_char=html.find("graphs") + 6),
        Chunk(resource_id=page.id, title="b", content="Trees &amp;", order=1,
              start_char=3, end_char=14),
    ])
    session.commit()
    sync_all(session, MoodleAdapter(client), user_id)
    session.expire_all()
    found, gone = session.exec(select(Chunk).order_by(Chunk.order)).all()
    text = _resource(session, "11").extracted_text
    assert text[found.start_char:found.end_char] == "graphs"
    assert (gone.start_char, gone.end_char) == (None, None)  # not in the new text

@pytest.mark.parametrize("url, kind", [
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "video"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "video"),
    ("https://youtube.com/live/dQw4w9WgXcQ", "video"),
    ("https://www.youtube.com/@lecturer", "link"),
    ("https://www.youtube.com/playlist?list=PL1", "link"),
    ("https://evil.example/youtu.be/dQw4w9WgXcQ", "link"),
])
def test_link_type_youtube_forms(url, kind):
    assert link_type(url) == kind


@pytest.fixture()
def llm_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "k")


def _pipeline_jobs(session):
    from app.models import Job

    return session.exec(select(Job).where(Job.type == "pipeline")).all()


def test_first_sync_queues_a_pipeline_run_once(session, user_id, monkeypatch, llm_key):
    _sync_job(session, monkeypatch, FakeAdapter())
    job, = _pipeline_jobs(session)
    assert job.payload == {"source": "moodle", "user_email": "s@x.edu"}
    _sync_job(session, monkeypatch, FakeAdapter())  # still pending: not queued twice
    assert len(_pipeline_jobs(session)) == 1


def test_no_pipeline_run_once_the_user_has_chunks(session, user_id, monkeypatch, llm_key):
    sync_course(session, FakeAdapter(), "c1", user_id)
    page = session.exec(select(Resource).where(Resource.source_id == "r-page")).one()
    session.add(Chunk(resource_id=page.id, order=0, title="T", content="A tree is..."))
    session.commit()
    _sync_job(session, monkeypatch, FakeAdapter())
    assert _pipeline_jobs(session) == []


def test_no_pipeline_run_without_an_llm_key(session, user_id, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    _sync_job(session, monkeypatch, FakeAdapter())
    assert _pipeline_jobs(session) == []


def test_another_users_chunks_dont_count(session, user_id, monkeypatch, llm_key):
    other = User(email="o@x.edu")
    session.add(other)
    session.commit()
    sync_course(session, FakeAdapter(), "c1", other.id)
    page = session.exec(select(Resource).where(Resource.source_id == "r-page")).first()
    session.add(Chunk(resource_id=page.id, order=0, title="T", content="A tree is..."))
    session.commit()
    _sync_job(session, monkeypatch, FakeAdapter())
    assert len(_pipeline_jobs(session)) == 1


class _RevokedGrant(FakeAdapter):
    def fetch_courses(self):
        from google.auth.exceptions import RefreshError

        raise RefreshError("invalid_grant: Token has been expired or revoked.")


def _classroom_job(session, monkeypatch, adapter):
    import app.sync_cli as sync_cli
    from app.jobs import run_sync_job

    monkeypatch.setattr(sync_cli, "build_adapter", lambda source, user: adapter)
    return run_sync_job(session, {"source": "classroom", "course_id": None,
                                  "user_email": "s@x.edu"})


def _store_token(session, user_id, token):
    from app.auth import _REFRESH_PURPOSE
    from app.crypto import seal

    user = session.get(User, user_id)
    user.google_refresh_token = seal(_REFRESH_PURPOSE, token)
    session.add(user)
    session.commit()
    return user


def test_revoked_grant_forgets_the_token(session, user_id, monkeypatch):
    from app.auth import refresh_token_for
    from app.sync_cli import NotConnectedError

    user = _store_token(session, user_id, "DEAD")
    with pytest.raises(NotConnectedError, match="sign in again"):
        _classroom_job(session, monkeypatch, _RevokedGrant())
    session.refresh(user)
    assert refresh_token_for(user) is None


def test_revoked_grant_keeps_a_token_stored_meanwhile(session, user_id):
    from app.auth import forget_revoked_token, refresh_token_for

    user = _store_token(session, user_id, "FRESH")
    assert not forget_revoked_token(session, user, "DEAD")
    assert refresh_token_for(user) == "FRESH"


def test_other_refresh_errors_keep_the_token(session, user_id, monkeypatch):
    from google.auth.exceptions import RefreshError

    from app.auth import refresh_token_for

    class _Scope(FakeAdapter):
        def fetch_courses(self):
            raise RefreshError("invalid_scope")

    user = _store_token(session, user_id, "KEEP")
    with pytest.raises(RefreshError):
        _classroom_job(session, monkeypatch, _Scope())
    session.refresh(user)
    assert refresh_token_for(user) == "KEEP"


def test_revoked_grant_mid_sync_still_forgets_the_token(session, user_id, monkeypatch):
    from google.auth.exceptions import RefreshError

    from app.auth import refresh_token_for
    from app.sync_cli import NotConnectedError

    class _RevokedTopics(FakeAdapter):
        def fetch_topics(self, cid):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    user = _store_token(session, user_id, "DEAD")
    with pytest.raises(NotConnectedError):
        _classroom_job(session, monkeypatch, _RevokedTopics())
    session.refresh(user)
    assert refresh_token_for(user) is None


def test_revoked_grant_keeps_a_token_stored_after_the_read(session, user_id):
    from sqlmodel import update

    from app.auth import _REFRESH_PURPOSE, forget_revoked_token, refresh_token_for
    from app.crypto import seal

    user = _store_token(session, user_id, "DEAD")
    real_refresh = session.refresh

    def refresh_then_sign_in(obj, *a, **k):
        # a sign-in lands between the comparison's read and the write
        real_refresh(obj, *a, **k)
        session.connection().execute(
            update(User).where(User.id == user_id)
            .values(google_refresh_token=seal(_REFRESH_PURPOSE, "FRESH")))
        session.refresh = real_refresh

    session.refresh = refresh_then_sign_in
    assert not forget_revoked_token(session, user, "DEAD")
    assert refresh_token_for(user) == "FRESH"
