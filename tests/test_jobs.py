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


def _backlog(worker_engine, monkeypatch, slices: int) -> list:
    """Queue a pipeline job that hands itself back `slices - 1` times, like a
    long backlog. Its first slice ages the notify job by two hours."""
    order: list = []

    def pipeline(s, payload):
        order.append("slice")
        if order.count("slice") == 1:
            for job in s.exec(select(Job).where(Job.type == "send_notifications")).all():
                job.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
                s.add(job)
            s.commit()
        if order.count("slice") < slices:
            raise jobs.Defer(timedelta(0), "more to do")

    monkeypatch.setitem(HANDLERS, "pipeline", pipeline)
    monkeypatch.setitem(
        HANDLERS, "send_notifications", lambda s, p: order.append("notify")
    )
    with Session(worker_engine) as s:
        enqueue(s, "pipeline", {})
    return order


@pytest.mark.parametrize("notify_every", [None, timedelta(hours=1)])
def test_long_drain_queues_notify_again_when_due(worker_engine, monkeypatch, notify_every):
    order = _backlog(worker_engine, monkeypatch, slices=12)
    worker.run_once(notify_every)
    assert order.count("slice") == 12
    # one at the start, one when it fell due mid-drain: not one per batch
    assert order.count("notify") == 2
    assert order.index("notify") < 5 and order[-1] == "slice"


def test_drain_does_not_requeue_a_recent_notify(worker_engine, monkeypatch):
    order: list = []
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: order.append("fake"))
    monkeypatch.setitem(
        HANDLERS, "send_notifications", lambda s, p: order.append("notify")
    )
    with Session(worker_engine) as s:
        for _ in range(12):
            enqueue(s, "fake", {})
    worker.run_once()  # every pass: still one notify job, not one per batch
    assert order.count("notify") == 1


def test_long_drain_pings_between_batches(worker_engine, monkeypatch):
    pings: list = []
    monkeypatch.setattr(worker, "ping_healthcheck", lambda fail=False: pings.append(fail))
    monkeypatch.setattr(worker, "PING_EVERY", timedelta(0))
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: None)
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: None)
    monkeypatch.setitem(HANDLERS, "bad", lambda s, p: 1 / 0)
    with Session(worker_engine) as s:
        for _ in range(5):  # the first batch
            enqueue(s, "fake", {})
        enqueue(s, "bad", {}, max_attempts=1)
        for _ in range(5):
            enqueue(s, "fake", {})
    assert worker.run_once()["completed"] == 11
    # healthy after batch one; the failure in batch two is reported at once
    # and not papered over by a later "alive" ping
    assert pings == [False, True, True, True]


def test_short_pass_pings_once(worker_engine, monkeypatch):
    pings: list = []
    monkeypatch.setattr(worker, "ping_healthcheck", lambda fail=False: pings.append(fail))
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: None)
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: None)
    with Session(worker_engine) as s:
        for _ in range(12):
            enqueue(s, "fake", {})
    worker.run_once()
    assert pings == [False]  # batches seconds apart don't each ping


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


def test_pipeline_job_skips_archived_courses(session, pipeline_user, monkeypatch):
    from app.models import Course

    ran = _fake_pipeline(monkeypatch)
    _set(session, session.get(Course, pipeline_user[0]), archived=True)
    out = jobs.run_pipeline_job(session, {"source": "moodle", "user_email": "s@x.edu"})
    assert ran == [("moodle", pipeline_user[1])]
    assert list(out) == [str(pipeline_user[1])]


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


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="advisory locks need Postgres")
def test_sync_job_defers_while_another_sync_of_the_user_runs(session, monkeypatch):
    import app.jobs as jobs
    import app.pipeline as pipeline

    monkeypatch.setattr(jobs, "_sync", lambda session, payload: {})
    job = enqueue(session, "sync", {"source": "moodle", "user_email": "s@x.edu",
                                    "course_id": "B"})
    other = enqueue(session, "sync", {"source": "moodle", "user_email": "o@x.edu",
                                      "course_id": None})
    with pipeline.advisory_lock("app.sync:moodle:s@x.edu", make_engine()) as held:
        assert held  # e.g. a course A sync of the same user mid-run
        assert run_due(session) == {"deferred": 1, "completed": 1, "failed": 0, "retried": 0}
    assert session.get(Job, other.id).status == "completed"
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


@pytest.fixture()
def no_shared_keys(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "moodle_token", "")
    monkeypatch.setattr(settings, "google_refresh_token", "")


def _sync_jobs(session):
    session.expire_all()
    return session.exec(select(Job).where(Job.type == "sync").order_by(Job.created_at)).all()


def test_request_sync_queues_each_connected_source(session, no_shared_keys):
    from app.auth import sign_in
    from app.models import User
    from app.moodle_tokens import encrypt_token

    nobody = User(email="n@x.edu")
    session.add(nobody)
    session.commit()
    assert jobs.request_sync(session, nobody) == ("disconnected", None)
    assert _sync_jobs(session) == []

    user = sign_in(session, "s@x.edu", "refresh-token")  # Classroom only
    assert jobs.request_sync(session, user) == ("queued", None)
    assert [j.payload for j in _sync_jobs(session)] == [
        {"source": "classroom", "user_email": "s@x.edu", "course_id": None}]

    both = sign_in(session, "b@x.edu", "refresh-token")
    _set(session, both, moodle_token=encrypt_token("key"))
    assert jobs.request_sync(session, both) == ("queued", None)
    mine = [j for j in _sync_jobs(session) if j.payload["user_email"] == "b@x.edu"]
    assert sorted(j.payload["source"] for j in mine) == ["classroom", "moodle"]
    assert all(j.status == "pending" for j in mine)


def test_request_sync_once_per_ten_minutes(session, no_shared_keys):
    from app.auth import sign_in

    user = sign_in(session, "s@x.edu", "refresh-token")
    other = sign_in(session, "o@x.edu", "refresh-token")
    assert jobs.request_sync(session, user)[0] == "queued"
    assert jobs.request_sync(session, user) == ("syncing", None)  # still pending
    _set(session, _sync_jobs(session)[0], status="running")
    assert jobs.request_sync(session, user) == ("syncing", None)
    assert len(_sync_jobs(session)) == 1

    queued_at = datetime.now(timezone.utc)
    _set(session, _sync_jobs(session)[0], status="completed", created_at=queued_at)
    outcome, wait = jobs.request_sync(session, user, now=queued_at + timedelta(minutes=4))
    assert outcome == "wait" and wait == timedelta(minutes=6)
    assert len(_sync_jobs(session)) == 1
    # someone else's sync doesn't use up this user's turn, nor theirs ours
    assert jobs.request_sync(session, other)[0] == "queued"

    later = queued_at + jobs.MANUAL_SYNC_EVERY
    assert jobs.request_sync(session, user, now=later) == ("queued", None)
    assert [j.status for j in _sync_jobs(session)
            if j.payload["user_email"] == "s@x.edu"] == ["completed", "pending"]


def test_request_sync_counts_a_failed_or_cron_sync_too(session, no_shared_keys):
    from app.auth import sign_in

    user = sign_in(session, "s@x.edu", "refresh-token")
    # e.g. the nightly cron's, which just failed: retrying at once won't help
    session.add(Job(type="sync", status="failed",
                    payload={"source": "classroom", "user_email": "s@x.edu"}))
    session.commit()
    assert jobs.request_sync(session, user)[0] == "wait"
    _set(session, _sync_jobs(session)[0],
         created_at=datetime.now(timezone.utc) - timedelta(minutes=11))
    assert jobs.request_sync(session, user) == ("queued", None)


def _connect(testapp, **fields):
    from app.models import User
    from app.moodle_tokens import encrypt_token

    with testapp["Session"]() as s:
        user = s.get(User, testapp["user_id"])
        user.moodle_token = encrypt_token("key")
        for k, v in fields.items():
            setattr(user, k, v)
        s.add(user)
        s.commit()


def _home_token(client):
    import re

    return re.search(r'name="csrf_token" value="([^"]+)"', client.get("/").text).group(1)


SYNC_BUTTON = 'action="/sync"'


def test_sync_now_button_queues_a_sync(testapp, no_shared_keys):
    from app.models import Course

    client = testapp["client"]
    _connect(testapp)
    home = client.get("/").text
    assert "No courses synced yet" in home and SYNC_BUTTON in home

    r = client.post("/sync", data={"csrf_token": _home_token(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    with testapp["Session"]() as s:
        (job,) = _sync_jobs(s)
        assert job.status == "pending"
        assert job.payload == {"source": "moodle", "user_email": "test@x.edu",
                               "course_id": None}
    home = client.get("/").text
    assert "Your courses are syncing" in home  # the empty state says so: no flash
    assert SYNC_BUTTON not in home and 'class="notice' not in home

    # a stale tab's button, pressed again while that one is queued
    client.post("/sync", data={"csrf_token": _home_token(client)})
    with testapp["Session"]() as s:
        assert len(_sync_jobs(s)) == 1
        s.add(Course(user_id=testapp["user_id"], source="moodle", source_id="c1",
                     name="Maths"))
        s.commit()
    home = client.get("/").text  # with courses listed, a banner says it instead
    assert "Syncing your courses now" in home and "Maths" in home
    assert SYNC_BUTTON not in home


def test_sync_now_is_rate_limited(testapp, no_shared_keys):
    client = testapp["client"]
    _connect(testapp)
    token = _home_token(client)
    client.post("/sync", data={"csrf_token": token})
    with testapp["Session"]() as s:
        _set(s, _sync_jobs(s)[0], status="completed")

    assert SYNC_BUTTON in client.get("/").text  # finished: the button is back
    r = client.post("/sync", data={"csrf_token": token})  # follows to /
    assert "You can sync again in 10 minutes." in r.text
    assert "sync again" not in client.get("/").text  # shown once
    with testapp["Session"]() as s:
        assert len(_sync_jobs(s)) == 1
        _set(s, _sync_jobs(s)[0],
             created_at=datetime.now(timezone.utc) - timedelta(minutes=9, seconds=30))
    assert "You can sync again in 1 minute." in client.post(
        "/sync", data={"csrf_token": token}).text

    with testapp["Session"]() as s:
        _set(s, _sync_jobs(s)[0],
             created_at=datetime.now(timezone.utc) - timedelta(minutes=10, seconds=1))
    client.post("/sync", data={"csrf_token": token})
    with testapp["Session"]() as s:
        assert [j.status for j in _sync_jobs(s)] == ["completed", "pending"]


def test_sync_now_needs_a_connection_and_a_csrf_token(testapp, no_shared_keys):
    client = testapp["client"]
    home = client.get("/").text
    assert "No account connected" in home and SYNC_BUTTON not in home
    token = _home_token(client)  # the sign-out form's
    r = client.post("/sync", data={"csrf_token": token})
    assert "Nothing to sync yet" in r.text
    _connect(testapp)
    assert client.post("/sync", data={}).status_code == 403
    assert client.post("/sync", data={"csrf_token": "wrong"}).status_code == 403
    with testapp["Session"]() as s:
        assert _sync_jobs(s) == []
