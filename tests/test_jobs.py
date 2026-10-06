"""Job queue tests: ordering, success, retry-with-backoff, terminal failure,
stale-running reaper, pruning, graceful shutdown, and the worker's single
notify path and loop."""

import signal
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app import jobs, worker
from app.jobs import HANDLERS, RUNNING_TIMEOUT, Shutdown, enqueue, prune_finished, run_due
from app.models import Job
from tests.dbutil import TEST_DATABASE_URL, make_engine


@pytest.fixture(autouse=True)
def _no_stop():
    jobs.STOP.clear()
    yield
    jobs.STOP.clear()


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def fake_handler(monkeypatch):
    calls: list = []

    def fake(session, payload):
        calls.append(payload)
        if payload.get("fail"):
            raise RuntimeError("boom")
        return {"ok": True}

    monkeypatch.setitem(HANDLERS, "fake", fake)
    return calls


def test_success_completed_with_result(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    out = run_due(session)
    assert out == {"completed": 1, "failed": 0, "retried": 0}
    row = session.get(Job, job.id)
    assert row.status == "completed" and row.payload["result"] == {"ok": True}
    assert fake_handler == [{"n": 1}]


def test_oldest_first_and_limit(session, fake_handler):
    enqueue(session, "fake", {"n": 1})
    enqueue(session, "fake", {"n": 2})
    run_due(session, limit=1)
    assert fake_handler == [{"n": 1}]
    run_due(session)
    assert fake_handler == [{"n": 1}, {"n": 2}]


def test_failure_retries_then_completes(session, fake_handler):
    job = enqueue(session, "fake", {"fail": True}, max_attempts=3)
    out = run_due(session)
    assert out["retried"] == 1
    row = session.get(Job, job.id)
    assert row.status == "pending" and row.attempts == 1
    # naive on SQLite, aware on Postgres
    assert "boom" in (row.error or "")
    assert row.available_at.replace(tzinfo=None) > datetime(2020, 1, 1)
    # make it due again with a fixed payload -> succeeds
    row.available_at = datetime(2000, 1, 1)
    row.payload = {}
    session.add(row)
    session.commit()
    out = run_due(session)
    assert out["completed"] == 1
    assert session.get(Job, job.id).status == "completed"


def test_exhausted_attempts_fail_terminally(session, fake_handler):
    job = enqueue(session, "fake", {"fail": True}, max_attempts=1)
    out = run_due(session)
    assert out == {"completed": 0, "failed": 1, "retried": 0}
    row = session.get(Job, job.id)
    assert row.status == "failed" and "boom" in (row.error or "")


def test_unknown_type_fails_with_error(session):
    job = enqueue(session, "nope", {}, max_attempts=1)
    run_due(session)
    row = session.get(Job, job.id)
    assert row.status == "failed" and "no handler" in (row.error or "")


def test_empty_queue_noop(session):
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0}


def _set(session, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    session.add(job)
    session.commit()


def test_future_job_not_claimed(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    _set(session, job, available_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert run_due(session)["completed"] == 0
    assert fake_handler == []


def test_stale_running_job_reaped_and_rerun(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    old = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=1)
    _set(session, job, status="running", attempts=1, updated_at=old)
    out = run_due(session)
    assert out["reaped"] == 1 and out["completed"] == 1
    row = session.get(Job, job.id)
    assert row.status == "completed" and row.attempts == 2


def test_stale_running_job_out_of_attempts_fails(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1}, max_attempts=1)
    old = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=1)
    _set(session, job, status="running", attempts=1, updated_at=old)
    run_due(session)
    row = session.get(Job, job.id)
    assert row.status == "failed" and "timed out" in row.error
    assert fake_handler == []


def test_fresh_running_job_left_alone(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    _set(session, job, status="running", attempts=1)
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0}
    assert session.get(Job, job.id).status == "running"


def test_handler_db_error_rolled_back_and_recorded(session, monkeypatch):
    def broken(s, payload):
        s.add(Job(type=None, payload={}))  # NOT NULL violation on flush
        s.flush()

    monkeypatch.setitem(HANDLERS, "broken", broken)
    job = enqueue(session, "broken", {}, max_attempts=1)
    assert run_due(session)["failed"] == 1
    row = session.get(Job, job.id)
    assert row.status == "failed" and "IntegrityError" in row.error
    assert len(session.exec(select(Job)).all()) == 1


@pytest.fixture()
def worker_engine(monkeypatch):
    from sqlalchemy.pool import StaticPool

    engine = make_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "ping_healthcheck", lambda fail=False: None)
    return engine


def test_worker_drains_queue_and_notifies_once(worker_engine, monkeypatch):
    order: list = []
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: order.append(p["n"]))
    monkeypatch.setitem(
        HANDLERS, "send_notifications", lambda s, p: order.append("notify")
    )
    with Session(worker_engine) as s:
        for n in range(8):  # more than one run_due batch
            enqueue(s, "fake", {"n": n})
    out = worker.run_once()
    assert out["completed"] == 9
    assert order == [*range(8), "notify"]  # syncs first, one notify pass
    worker.run_once()
    assert order.count("notify") == 2


def test_worker_skips_enqueue_when_notify_already_queued(worker_engine, monkeypatch):
    calls: list = []
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: calls.append(1))
    with Session(worker_engine) as s:
        enqueue(s, "send_notifications")
    worker.run_once()
    assert calls == [1]
    with Session(worker_engine) as s:
        assert len(s.exec(select(Job)).all()) == 1


def test_worker_throttles_notify_job_to_notify_every(worker_engine, monkeypatch):
    calls: list = []
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: calls.append(1))
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: None)
    hour = timedelta(hours=1)
    worker.run_once(hour)  # no notify job yet: queue one
    assert calls == [1]
    with Session(worker_engine) as s:
        enqueue(s, "fake", {})  # a sign-in sync between polls
    out = worker.run_once(hour)
    assert out["completed"] == 1 and calls == [1]  # sync ran, no second scan
    with Session(worker_engine) as s:
        job = s.exec(select(Job).where(Job.type == "send_notifications")).one()
        job.created_at = datetime.now(timezone.utc) - hour - timedelta(seconds=1)
        s.add(job)
        s.commit()
    worker.run_once(hour)  # the last one is over an hour old
    assert calls == [1, 1]


def test_due_sync_claimed_before_earlier_notify(session, fake_handler, monkeypatch):
    order: list = []
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: order.append("notify"))
    enqueue(session, "send_notifications")
    enqueue(session, "fake", {"n": 1})
    run_due(session, limit=1)
    assert fake_handler == [{"n": 1}] and order == []
    run_due(session)
    assert order == ["notify"]


def test_notify_waits_for_sync_running_elsewhere(session, fake_handler, monkeypatch):
    order: list = []
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: order.append("notify"))
    sync = enqueue(session, "fake", {"n": 1})
    _set(session, sync, status="running", attempts=1)  # another worker's claim
    enqueue(session, "send_notifications")
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0}
    _set(session, sync, status="completed")
    run_due(session)
    assert order == ["notify"]


def test_only_one_active_notify_job(session):
    enqueue(session, "send_notifications")
    with pytest.raises(IntegrityError):
        enqueue(session, "send_notifications")
    session.rollback()
    row = session.exec(select(Job)).one()
    _set(session, row, status="completed")
    enqueue(session, "send_notifications")  # finished ones don't count


@pytest.fixture()
def file_engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    SQLModel.metadata.create_all(engine)
    return engine


def test_result_from_reclaimed_job_rejected(file_engine, monkeypatch):
    def overtaken(s, payload):
        # Another worker reaps this job's lapsed lease and claims it again.
        with Session(file_engine) as other:
            row = other.get(Job, job_id)
            row.attempts += 1
            other.add(row)
            other.commit()
        return {"stale": True}

    monkeypatch.setitem(HANDLERS, "overtaken", overtaken)
    with Session(file_engine) as s:
        job_id = enqueue(s, "overtaken", {}).id
        out = run_due(s)
    assert out["lost"] == 1 and out["completed"] == 0
    with Session(file_engine) as s:
        row = s.get(Job, job_id)
    assert row.status == "running" and row.attempts == 2
    assert "result" not in row.payload


def test_heartbeat_renews_lease_while_handler_runs(file_engine, monkeypatch):
    import time

    seen: list = []

    def slow(s, payload):
        for _ in range(2):
            with Session(file_engine) as other:
                seen.append(other.get(Job, job_id).updated_at)
            time.sleep(0.2)
        return {}

    monkeypatch.setattr(jobs, "HEARTBEAT_EVERY", timedelta(seconds=0.05))
    monkeypatch.setitem(HANDLERS, "slow", slow)
    with Session(file_engine) as s:
        job_id = enqueue(s, "slow", {}).id
        assert run_due(s)["completed"] == 1
    assert seen[1] > seen[0]


def test_worker_replaces_orphaned_notify_job_same_pass(worker_engine, monkeypatch):
    calls: list = []
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: calls.append(1))
    old = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=1)
    with Session(worker_engine) as s:
        job = enqueue(s, "send_notifications", max_attempts=1)
        _set(s, job, status="running", attempts=1, updated_at=old)
    out = worker.run_once()
    assert calls == [1] and out["reaped"] == 1
    with Session(worker_engine) as s:
        assert sorted(j.status for j in s.exec(select(Job)).all()) == ["completed", "failed"]


def test_shutdown_hands_running_job_back_with_attempt_refunded(session, monkeypatch):
    def interrupted(s, payload):
        raise Shutdown("SIGTERM")

    monkeypatch.setitem(HANDLERS, "long", interrupted)
    job = enqueue(session, "long", {})
    with pytest.raises(Shutdown):
        run_due(session)
    row = session.get(Job, job.id)
    session.refresh(row)
    assert row.status == "pending" and row.attempts == 0
    assert row.error == "interrupted by worker shutdown"
    assert not jobs.in_handler


def test_stop_during_claim_hands_job_back_unrun(session, fake_handler, monkeypatch):
    claim = jobs._claim_next

    def claim_then_sigterm(s):
        job = claim(s)
        jobs.STOP.set()  # signal lands mid-claim, before in_handler is set
        return job

    monkeypatch.setattr(jobs, "_claim_next", claim_then_sigterm)
    job = enqueue(session, "fake", {"n": 1})
    with pytest.raises(Shutdown):
        run_due(session)
    assert fake_handler == []
    row = session.get(Job, job.id)
    session.refresh(row)
    assert row.status == "pending" and row.attempts == 0


def test_stop_flag_stops_claiming(session, fake_handler):
    enqueue(session, "fake", {"n": 1})
    jobs.STOP.set()
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0}
    assert fake_handler == []


def test_signal_interrupts_only_inside_a_handler(monkeypatch):
    worker._request_stop(signal.SIGTERM, None)  # between jobs: just flag it
    assert jobs.STOP.is_set()
    monkeypatch.setattr(jobs, "in_handler", True)
    with pytest.raises(Shutdown):
        worker._request_stop(signal.SIGTERM, None)


def test_prune_finished_drops_only_old_finished_jobs(session):
    old = datetime.now(timezone.utc) - timedelta(days=31)
    rows = {}
    for status in ("completed", "failed", "pending", "running"):
        rows[status] = enqueue(session, "fake", {})
        _set(session, rows[status], status=status, updated_at=old)
    recent = enqueue(session, "fake", {})
    _set(session, recent, status="completed")
    assert prune_finished(session, timedelta(days=30)) == 2
    left = sorted(j.status for j in session.exec(select(Job)).all())
    assert left == ["completed", "pending", "running"]


def test_worker_pings_fail_when_a_job_fails(worker_engine, monkeypatch):
    pings: list = []
    monkeypatch.setattr(worker, "ping_healthcheck", lambda fail=False: pings.append(fail))
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: None)
    monkeypatch.setitem(HANDLERS, "bad", lambda s, p: 1 / 0)
    with Session(worker_engine) as s:
        enqueue(s, "bad", {}, max_attempts=1)
    assert worker.run_once()["failed"] == 1
    worker.run_once()
    assert pings == [True, False]


def test_worker_pings_fail_only_when_a_reaped_job_fails(worker_engine, monkeypatch):
    pings: list = []
    monkeypatch.setattr(worker, "ping_healthcheck", lambda fail=False: pings.append(fail))
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: None)
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: None)
    old = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=1)
    with Session(worker_engine) as s:
        _set(s, enqueue(s, "fake", {}), status="running", attempts=1, updated_at=old)
    out = worker.run_once()  # requeued and rerun: healthy
    assert out["reaped"] == 1 and out["failed"] == 0
    with Session(worker_engine) as s:
        _set(s, enqueue(s, "fake", {}, max_attempts=1),
             status="running", attempts=1, updated_at=old)
    out = worker.run_once()  # out of attempts: a terminal failure
    assert out["reaped"] == 1 and out["failed"] == 1
    assert pings == [False, True]


def test_worker_pings_fail_when_the_pass_crashes(worker_engine, monkeypatch):
    pings: list = []
    monkeypatch.setattr(worker, "ping_healthcheck", lambda fail=False: pings.append(fail))

    def db_down(session):
        raise ConnectionError("postgres restarting")

    monkeypatch.setattr(worker, "reap_stale", db_down)
    with pytest.raises(ConnectionError):
        worker.run_once()
    assert pings == [True]


class _InstantStop(type(jobs.STOP)):
    def wait(self, timeout=None):  # don't really sleep between passes
        return self.is_set()


def _main(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["app.worker", *argv])
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(jobs, "STOP", _InstantStop())
    worker.main()


def test_loop_survives_a_failed_pass(monkeypatch):
    calls: list = []

    def run_once(notify_every=None):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("postgres restarting")
        if len(calls) == 3:
            jobs.STOP.set()  # SIGTERM between passes
        return {}

    monkeypatch.setattr(worker, "run_once", run_once)
    _main(monkeypatch, "--loop", "60")
    assert len(calls) == 3


def test_loop_defaults_to_hourly_notify(monkeypatch):
    seen: list = []

    def run_once(notify_every=None):
        seen.append(notify_every)
        jobs.STOP.set()
        return {}

    monkeypatch.setattr(worker, "run_once", run_once)
    _main(monkeypatch, "--loop", "60")
    _main(monkeypatch)  # a single (cron) pass always notifies
    _main(monkeypatch, "--loop", "60", "--notify-every", "600")
    assert seen == [timedelta(hours=1), None, timedelta(minutes=10)]


def test_main_logs_the_commit(monkeypatch, caplog):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "8490f56abc")
    monkeypatch.setattr(worker, "run_once", lambda notify_every=None: {})
    with caplog.at_level("INFO", logger="app.worker"):
        _main(monkeypatch)
    assert "starting worker at commit 8490f56abc" in caplog.text


def test_single_pass_still_raises(monkeypatch):
    def run_once(notify_every=None):
        raise ConnectionError("postgres restarting")

    monkeypatch.setattr(worker, "run_once", run_once)
    with pytest.raises(ConnectionError):
        _main(monkeypatch)


def test_main_exits_cleanly_when_a_job_is_interrupted(monkeypatch):
    def run_once(notify_every=None):
        jobs.STOP.set()
        raise Shutdown("SIGTERM")

    monkeypatch.setattr(worker, "run_once", run_once)
    _main(monkeypatch, "--loop", "60")  # no exception escapes


def test_deferred_job_waits_with_attempt_refunded(session, monkeypatch):
    def busy(s, payload):
        raise jobs.Defer(timedelta(minutes=30), "lock taken")

    monkeypatch.setitem(HANDLERS, "busy", busy)
    job = enqueue(session, "busy", {})
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0, "deferred": 1}
    row = session.get(Job, job.id)
    session.refresh(row)
    assert row.status == "pending" and row.attempts == 0 and row.error == "lock taken"
    assert row.available_at.replace(tzinfo=timezone.utc) > (
        datetime.now(timezone.utc) + timedelta(minutes=29))
    # not due yet
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0}


def test_worker_pass_ends_with_a_deferred_job(worker_engine, monkeypatch):
    def busy(s, payload):
        raise jobs.Defer(timedelta(minutes=30), "lock taken")

    monkeypatch.setitem(HANDLERS, "busy", busy)
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: None)
    with Session(worker_engine) as s:
        enqueue(s, "busy", {})
    out = worker.run_once()
    assert out["deferred"] == 1 and out["completed"] == 1  # the notify pass


@pytest.fixture()
def pipeline_user(session):
    from app.models import Course, User

    user, other = User(email="s@x.edu"), User(email="o@x.edu")
    session.add_all([user, other])
    session.commit()
    mine = [Course(source="moodle", source_id=f"c{i}", name="C", user_id=user.id)
            for i in range(2)]
    session.add_all([*mine,
                     Course(source="classroom", source_id="g", name="G", user_id=user.id),
                     Course(source="moodle", source_id="c0", name="C", user_id=other.id)])
    session.commit()
    return sorted(c.id for c in mine)


def _fake_pipeline(monkeypatch, quota_on=None, got=True):
    from contextlib import contextmanager

    import app.pipeline as pipeline
    from app.pipeline import StageResult

    ran: list = []

    def run_course(s, source, course_id, chunk_llm, quiz_llm, pace=0.0, deadline=None):
        ran.append((source, course_id))
        r = StageResult()
        r.counts["chunks"] = 2
        r.quota_exhausted = course_id == quota_on
        return {"chunking": r}

    @contextmanager
    def single_run(source, bind=None):
        yield got

    monkeypatch.setattr(pipeline, "run_course", run_course)
    monkeypatch.setattr(pipeline, "single_run", single_run)
    monkeypatch.setattr(pipeline, "llm_clients", lambda: (None, None))
    return ran


def test_pipeline_job_runs_only_the_users_courses_of_its_source(
        session, pipeline_user, monkeypatch):
    ran = _fake_pipeline(monkeypatch)
    out = jobs.run_pipeline_job(session, {"source": "moodle", "user_email": "s@x.edu"})
    assert ran == [("moodle", cid) for cid in pipeline_user]
    assert out == {str(cid): {"chunking": {"chunks": 2}} for cid in pipeline_user}


def test_pipeline_job_stops_when_the_quota_runs_out(session, pipeline_user, monkeypatch):
    ran = _fake_pipeline(monkeypatch, quota_on=pipeline_user[0])
    out = jobs.run_pipeline_job(session, {"source": "moodle", "user_email": "s@x.edu"})
    assert ran == [("moodle", pipeline_user[0])]
    assert out[str(pipeline_user[0])]["chunking"]["quota_exhausted"] is True


def test_pipeline_job_defers_while_another_run_holds_the_lock(
        session, pipeline_user, monkeypatch):
    ran = _fake_pipeline(monkeypatch, got=False)
    job = enqueue(session, "pipeline", {"source": "moodle", "user_email": "s@x.edu"})
    assert run_due(session)["deferred"] == 1
    assert ran == []
    row = session.get(Job, job.id)
    session.refresh(row)
    assert row.status == "pending" and "in progress" in row.error


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="advisory locks need Postgres")
def test_pipeline_job_defers_behind_a_real_pipeline_lock(session, pipeline_user, monkeypatch):
    import app.pipeline as pipeline

    monkeypatch.setattr(pipeline, "llm_clients", lambda: (None, None))
    monkeypatch.setattr(pipeline, "run_course", lambda *a, **kw: {})
    job = enqueue(session, "pipeline", {"source": "moodle", "user_email": "s@x.edu"})
    with pipeline.single_run("moodle", make_engine()) as cron:  # e.g. the 06:00 run
        assert cron
        assert run_due(session)["deferred"] == 1
    _set(session, session.get(Job, job.id), available_at=datetime.now(timezone.utc))
    assert run_due(session)["completed"] == 1  # lock released: it runs


@pytest.mark.parametrize("setup, title", [
    ("none", "No account connected"),
    ("pending", "Your courses are syncing"),
    ("running", "Your courses are syncing"),
    ("failed", "Your last sync failed"),
    ("completed", "No courses found"),
    ("connected_no_history", "No courses synced yet"),
    ("other_user_pending", "No account connected"),
])
def test_empty_course_list_explains_why(testapp, monkeypatch, setup, title):
    from app.config import settings
    from app.models import Course, User
    from app.moodle_tokens import REJECTED, encrypt_token

    monkeypatch.setattr(settings, "moodle_token", "")
    monkeypatch.setattr(settings, "google_refresh_token", "")
    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        if setup in ("failed", "completed", "connected_no_history"):
            user.moodle_token = encrypt_token("key")
            s.add(user)
        if setup in ("pending", "running", "failed", "completed"):
            s.add(Job(type="sync", status=setup,
                      payload={"source": "moodle", "user_email": user.email}))
        if setup == "failed":  # an older success doesn't hide the latest failure
            s.add(Job(type="sync", status="completed",
                      updated_at=datetime.now(timezone.utc) - timedelta(days=1),
                      payload={"source": "moodle", "user_email": user.email}))
        if setup == "other_user_pending":
            user.moodle_token = REJECTED  # a dead key counts as not connected
            s.add(user)
            s.add(Job(type="sync", status="pending",
                      payload={"source": "moodle", "user_email": "someone@x.edu"}))
        s.commit()
    page = testapp["client"].get("/").text
    assert title in page
    assert "sync_cli" not in page
    if setup in ("none", "failed", "other_user_pending", "connected_no_history"):
        assert 'href="/settings/moodle"' in page

    with testapp["Session"]() as s:  # with courses, none of it shows
        s.add(Course(user_id=testapp["user_id"], source="moodle", source_id="c1", name="Maths"))
        s.commit()
    page = testapp["client"].get("/").text
    assert "Maths" in page and title not in page


def test_job_deferred_by_zero_goes_behind_due_jobs(session, monkeypatch):
    order: list = []

    def once(s, payload):
        order.append("slice")
        if order.count("slice") == 1:
            raise jobs.Defer(timedelta(0), "slice used up")

    monkeypatch.setitem(HANDLERS, "slow", once)
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: order.append("fake"))
    enqueue(session, "slow", {})
    enqueue(session, "fake", {"n": 1})  # queued later, but due before the hand-back
    out = run_due(session)
    assert out["deferred"] == 1 and out["completed"] == 2
    assert order == ["slice", "fake", "slice"]


def test_notify_does_not_wait_for_a_pipeline_job(session, monkeypatch):
    order: list = []
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: order.append("notify"))
    pipeline_job = enqueue(session, "pipeline", {"source": "moodle", "user_email": "s@x.edu"})
    _set(session, pipeline_job, status="running", attempts=1)  # mid-slice elsewhere
    enqueue(session, "send_notifications")
    assert run_due(session)["completed"] == 1 and order == ["notify"]


def test_pipeline_job_runs_in_slices_behind_other_jobs(session, monkeypatch):
    import time
    from collections import Counter

    import app.pipeline as pipeline
    from app.config import settings
    from app.models import Chunk, Course, QuizItem, Resource, Topic, User
    from tests.test_pipeline import FakeLLM as ChunkLLM
    from tests.test_quiz import GOOD
    from tests.test_quiz import FakeLLM as QuizLLM

    user = User(email="s@x.edu")
    session.add(user)
    session.commit()
    course = Course(source="moodle", source_id="c1", name="C", user_id=user.id)
    session.add(course)
    session.commit()
    topic = Topic(course_id=course.id, source_id="t1", title="T")
    session.add(topic)
    session.commit()
    for i in range(3):
        session.add(Resource(topic_id=topic.id, source="moodle", source_id=f"r{i}",
                             type="file", title=f"R{i}", status="extracted",
                             extracted_text=f"Section {i} about trees and graphs."))
    session.commit()

    ticks = iter(range(10**6))
    monkeypatch.setattr(time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(jobs, "PIPELINE_SLICE", timedelta(seconds=2.5))  # ~2 items a slice
    monkeypatch.setattr(settings, "llm_pace", 0.0)
    monkeypatch.setattr(pipeline, "llm_clients", lambda: (ChunkLLM(), QuizLLM(GOOD[:2])))
    ran: list = []
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: ran.append(p))

    job = enqueue(session, "pipeline", {"source": "moodle", "user_email": "s@x.edu"})
    enqueue(session, "fake", {"n": 1})  # e.g. another student's sign-in sync
    assert run_due(session, limit=1)["deferred"] == 1  # first slice, handed back
    assert run_due(session, limit=1)["completed"] == 1 and ran == [{"n": 1}]

    slices = 1
    while True:
        session.expire_all()
        if session.get(Job, job.id).status != "pending":
            break
        run_due(session, limit=1)
        slices += 1
        assert slices < 20
    session.expire_all()
    row = session.get(Job, job.id)
    assert row.status == "completed" and row.attempts == 1  # deferrals refunded
    assert slices > 2
    chunks = session.exec(select(Chunk)).all()
    assert len(chunks) == 3  # short texts: one chunk per resource, none twice
    assert len({c.resource_id for c in chunks}) == 3
    items = session.exec(select(QuizItem)).all()
    assert len(items) == 6  # 2 per chunk, none twice
    assert sorted(Counter(i.chunk_id for i in items).values()) == [2, 2, 2]
