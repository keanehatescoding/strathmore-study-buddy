"""Phase 1.5 tests: course browser renders synced data (SQLite + TestClient)."""

import uuid

from app.models import Course, Resource, Topic


def test_browser_flow(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        course = Course(user_id=testapp["user_id"], source="moodle", source_id="c1",
                        name="CS 301", code="CS 301")
        s.add(course)
        s.commit()
        s.refresh(course)
        topic = Topic(course_id=course.id, source_id="t1", title="Trees", order=0)
        s.add(topic)
        s.commit()
        s.refresh(topic)
        res = Resource(
            topic_id=topic.id, source="moodle", source_id="r1",
            type="file", title="trees.pdf", status="pending",
        )
        s.add(res)
        s.commit()
        s.refresh(res)
        cid, rid = str(course.id), str(res.id)

    r = client.get("/")
    assert r.status_code == 200 and "CS 301" in r.text

    r = client.get(f"/courses/{cid}")
    assert r.status_code == 200 and "Trees" in r.text and "trees.pdf" in r.text

    r = client.get(f"/resources/{rid}")
    assert r.status_code == 200 and "No text yet" in r.text

    assert client.get("/courses/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/health").json() == {"status": "ok"}


def test_unowned_course_hidden(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        course = Course(source="moodle", source_id="c9", name="Pre-auth course")
        s.add(course)
        s.commit()
        course_id = course.id
    home = client.get("/")
    assert home.status_code == 200 and "Pre-auth course" not in home.text
    assert client.get(f"/courses/{course_id}").status_code == 404


def _resource_with_text(testapp, text, owned=True):
    Session = testapp["Session"]
    with Session() as s:
        course = Course(user_id=testapp["user_id"] if owned else None,
                        source="moodle", source_id="c2", name="CS 302")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t2", title="Graphs", order=0)
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r2", type="file",
                       title="graphs.pdf", status="extracted", extracted_text=text)
        s.add(res)
        s.commit()
        return str(res.id)


def test_long_text_preview_notes_truncation_and_serves_full_text(testapp):
    from app.main import RESOURCE_PREVIEW_CHARS

    client = testapp["client"]
    text = "a" * RESOURCE_PREVIEW_CHARS + "TAIL"
    rid = _resource_with_text(testapp, text)

    page = client.get(f"/resources/{rid}").text
    assert "TAIL" not in page
    assert f"of {len(text):,} characters" in page
    assert f'data-full-text-url="/resources/{rid}/text"' in page

    r = client.get(f"/resources/{rid}/text")
    assert r.status_code == 200 and r.text == text
    assert r.headers["content-type"].startswith("text/plain")


def test_short_text_has_no_truncation_note(testapp):
    client = testapp["client"]
    rid = _resource_with_text(testapp, "short notes")
    page = client.get(f"/resources/{rid}").text
    assert "short notes" in page
    assert "Showing the first" not in page and "data-full-text-url" not in page


def test_full_text_hidden_from_other_users(testapp):
    client = testapp["client"]
    rid = _resource_with_text(testapp, "secret", owned=False)
    assert client.get(f"/resources/{rid}/text").status_code == 404


def _course_page_selects(testapp, n_topics):
    from sqlalchemy import event

    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        course = Course(user_id=testapp["user_id"], source="moodle",
                        source_id=f"n{n_topics}", name="N+1")
        s.add(course)
        s.commit()
        for i in range(n_topics):
            topic = Topic(course_id=course.id, source_id=f"t{i}", title=f"Topic {i}", order=i)
            s.add(topic)
            s.commit()
            for name in ("b", "a"):
                s.add(Resource(topic_id=topic.id, source="moodle", source_id=f"r{i}{name}",
                               type="file", title=f"{name}-{i}.pdf", status="pending"))
        s.commit()
        cid, engine = course.id, s.get_bind()
    statements = []
    listener = lambda *a: statements.append(a[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        page = client.get(f"/courses/{cid}").text
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    for i in range(n_topics):  # each topic lists its own resources, by title
        assert page.index(f"Topic {i}") < page.index(f"a-{i}.pdf") < page.index(f"b-{i}.pdf")
    return sum(st.lstrip().upper().startswith("SELECT") for st in statements)


def test_course_page_queries_do_not_grow_with_topics(testapp):
    assert _course_page_selects(testapp, 1) == _course_page_selects(testapp, 5)


def test_split_assignments_by_local_day():
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from app.main import split_assignments
    from app.models import Assignment

    nairobi = ZoneInfo("Africa/Nairobi")  # UTC+3
    now = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)  # 12:00 local

    def a(title, due):
        return Assignment(course_id=None, source_id=title, title=title, due_date=due)

    later = a("later", now + timedelta(days=5))
    soon = a("soon", now + timedelta(days=1))
    # due 07:00 local today: already past the hour, but today's work is still shown
    this_morning = a("this morning", datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc))
    last_week = a("last week", now - timedelta(days=7))
    yesterday = a("yesterday", datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc))
    undated = a("undated", None)
    naive = a("naive", datetime(2026, 10, 9))  # SQLite hands back naive UTC

    upcoming, past = split_assignments(
        [later, last_week, undated, soon, yesterday, this_morning, naive], nairobi, now)
    assert [x.title for x in upcoming] == ["this morning", "soon", "naive", "later", "undated"]
    assert [x.title for x in past] == ["yesterday", "last week"]


def test_course_page_groups_past_assignments(testapp):
    from datetime import datetime, timedelta, timezone

    from app.models import Assignment

    now = datetime.now(timezone.utc)
    with testapp["Session"]() as s:
        course = Course(user_id=testapp["user_id"], source="moodle", source_id="c1", name="C")
        s.add(course)
        s.commit()
        s.add_all([
            Assignment(course_id=course.id, source_id="a1", title="Old essay",
                       due_date=now - timedelta(days=30)),
            Assignment(course_id=course.id, source_id="a2", title="Next essay",
                       due_date=now + timedelta(days=3)),
        ])
        s.commit()
        cid = course.id
    page = testapp["client"].get(f"/courses/{cid}").text
    upcoming, _, past = page.partition("Past assignments (1)")
    assert "Next essay" in upcoming and "Old essay" not in upcoming
    assert "Old essay" in past


def _resource_with_url(testapp, source, type_, raw_url):
    Session = testapp["Session"]
    with Session() as s:
        course = Course(user_id=testapp["user_id"], source=source, source_id="c3",
                        name="CS 303")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t3", title="Heaps", order=0)
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source=source, source_id="r3", type=type_,
                       title="heaps", status="pending", raw_url=raw_url)
        s.add(res)
        s.commit()
        return str(res.id)


def test_moodle_file_links_the_browser_url(testapp):
    rid = _resource_with_url(
        testapp, "moodle", "file",
        "https://m.example/moodle/webservice/pluginfile.php/9/mod_resource/content/1/h.pdf",
    )
    page = testapp["client"].get(f"/resources/{rid}").text
    assert 'href="https://m.example/moodle/pluginfile.php/9/mod_resource/content/1/h.pdf"' in page
    assert "webservice" not in page


def test_other_original_links_unchanged(testapp):
    url = "https://drive.google.com/file/d/abc/view"
    rid = _resource_with_url(testapp, "classroom", "file", url)
    assert f'href="{url}"' in testapp["client"].get(f"/resources/{rid}").text


def test_cap_note_is_shown_as_a_note_not_an_error(testapp):
    from app.extract import cap_text

    _, note = cap_text("x\n" * 400_000)
    rid = _resource_with_text(testapp, "kept part")
    with testapp["Session"]() as s:
        s.get(Resource, uuid.UUID(rid)).error = note
        s.commit()
    page = testapp["client"].get(f"/resources/{rid}").text
    assert f'<p class="muted">{note}</p>' in page and "resource-error" not in page


def _resource_with_status(testapp, status: str, error: str | None = None) -> str:
    with testapp["Session"]() as s:
        course = Course(user_id=testapp["user_id"], source="moodle", source_id="cs",
                        name="Status")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="ts", title="T", order=0)
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="rs", type="file",
                       title="file.pdf", status=status, error=error)
        s.add(res)
        s.commit()
        return str(res.id)


def test_resource_without_text_worded_by_status(testapp):
    client = testapp["client"]
    page = client.get(f"/resources/{_resource_with_status(testapp, 'pending')}").text
    assert "No text yet" in page and "Phase" not in page


def test_skipped_resource_gives_its_reason(testapp):
    rid = _resource_with_status(testapp, "skipped", "file is over 50 MB")
    page = testapp["client"].get(f"/resources/{rid}").text
    assert "This file was skipped, so there's no text to show: file is over 50 MB." in page
    assert "yet" not in page.split("Extracted text")[1]
    assert page.count("file is over 50 MB") == 1  # not repeated in the header


def test_failed_resource_says_it_failed(testapp):
    rid = _resource_with_status(testapp, "failed", "PDF is encrypted")
    page = testapp["client"].get(f"/resources/{rid}").text
    assert "We couldn't read the text from this file" in page
    assert "PDF is encrypted" in page and "No text yet" not in page
