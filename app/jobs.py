"""Postgres-backed job queue (Phase 7 hardening): sync runs go through here
instead of blocking the CLI caller.

- `enqueue(session, type, payload)` -> Job (status pending).
- `run_due(session, limit)` claims the oldest due pending jobs one at a time
  (pending -> running -> completed/failed) and executes them via HANDLERS.
  Claims use SELECT ... FOR UPDATE SKIP LOCKED, so concurrent workers never
  take the same job.
- Failed jobs retry with backoff until max_attempts, then stay failed with
  the error recorded. Nothing is silently dropped.
- A claim is a lease: while a handler runs, a heartbeat thread renews the
  job's updated_at every HEARTBEAT_EVERY. A job whose lease lapses for
  RUNNING_TIMEOUT (its worker died) is reaped back to pending, or to failed
  once its attempts are used up. `attempts` doubles as the claim token, so a
  worker whose claim was reaped and re-taken can't record its result.
- send_notifications is only claimed once no sync is running or due, on
  any worker, so it never notifies about half-synced data. A pipeline job
  doesn't hold it back: it runs for hours and only ever adds material.
- A worker told to stop (SIGTERM on redeploy) raises Shutdown into the
  running handler; the job goes straight back to pending with its attempt
  refunded, instead of waiting out the lease and burning a retry.
- A handler that can't run yet raises Defer: the job goes back to pending
  for a while with its attempt refunded. Claims go by available_at, so a
  job deferred by 0 (a pipeline job out of its time slice) lines up behind
  everything already due.
- prune_finished() deletes completed/failed jobs past their retention.
- Job types: "sync" (Moodle/Classroom course sync), "pipeline" (extract,
  chunk and quiz one user's courses after their first sync, so they needn't
  wait for the daily pipeline cron), "send_notifications".
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.models import Chunk, Course, Job, Resource, Topic, User

HANDLERS: dict[str, callable] = {}

# A running job's lease is renewed every HEARTBEAT_EVERY; one not renewed for
# RUNNING_TIMEOUT is presumed orphaned. Handler runtime itself is unbounded.
HEARTBEAT_EVERY = timedelta(minutes=1)
RUNNING_TIMEOUT = timedelta(minutes=10)


# A pipeline job that finds another run of its source going waits this long.
PIPELINE_BUSY_RETRY = timedelta(minutes=30)
# A pipeline job runs this long (plus the item in hand), then goes to the
# back of the queue, so sign-in syncs and notifications don't wait hours.
PIPELINE_SLICE = timedelta(minutes=10)


class Defer(Exception):
    """Raised by a handler that can't run yet (e.g. a lock is taken): the
    job is retried after `delay` without spending an attempt."""

    def __init__(self, delay: timedelta, reason: str):
        super().__init__(reason)
        self.delay = delay


class Shutdown(BaseException):
    """Raised into a running handler when the worker is asked to stop.
    A BaseException, so handlers' `except Exception` blocks don't eat it."""


# Set when the worker should stop: run_due claims nothing more. `in_handler`
# tells the signal handler whether raising Shutdown lands inside run_due's
# try (safe to hand the job back) or should wait for the next claim check.
STOP = threading.Event()
in_handler = False


def handler(job_type: str):
    def deco(fn):
        HANDLERS[job_type] = fn
        return fn

    return deco


def enqueue(
    session: Session, job_type: str, payload: dict | None = None,
    max_attempts: int = 3,
) -> Job:
    job = Job(type=job_type, payload=dict(payload or {}), max_attempts=max_attempts)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _enqueue_once(session: Session, job_type: str, source: str, user_email: str,
                  payload: dict) -> Job | None:
    """Queue `job_type` unless one is already pending or running for this
    source and user. Returns the new job, or None. The check is only a fast
    path: uq_jobs_active_user_job settles two callers that both pass it."""
    active = session.exec(
        select(Job).where(Job.type == job_type, Job.status.in_(("pending", "running")))
    ).all()
    if any(j.payload.get("source") == source and j.payload.get("user_email") == user_email
           for j in active):
        return None
    try:
        return enqueue(session, job_type,
                       {"source": source, "user_email": user_email, **payload})
    except IntegrityError:  # lost the race to another enqueue
        session.rollback()
        return None


def enqueue_sync_once(session: Session, source: str, user_email: str,
                      course_id: str | None = None) -> Job | None:
    """Queue a sync of `source` for the user (one course, or all when
    `course_id` is None) unless a sync is already pending or running for
    them. Returns the new job, or None."""
    return _enqueue_once(session, "sync", source, user_email, {"course_id": course_id})


def sync_state(session: Session, user: User) -> str:
    """Why a user with no courses has none, for the course list's empty
    state: "syncing" (a sync is pending or running), "disconnected" (no
    Moodle key or Classroom grant to sync with), "failed" (their latest
    sync failed), "unknown" (no sync on record: never queued, or pruned
    by prune_finished), else "empty" (synced fine, nothing to show)."""
    from app.sync_cli import has_credentials

    mine = session.exec(
        select(Job).where(Job.type == "sync",
                          Job.payload["user_email"].as_string() == user.email)
        .order_by(Job.updated_at.desc())
    ).all()
    if any(j.status in ("pending", "running") for j in mine):
        return "syncing"
    if not any(has_credentials(source, user) for source in ("moodle", "classroom")):
        return "disconnected"
    if not mine:
        return "unknown"
    return "failed" if mine[0].status == "failed" else "empty"


def has_chunks(session: Session, user_id, source: str) -> bool:
    """Whether any of the user's `source` courses has been chunked yet."""
    return session.exec(
        select(Chunk.id)
        .join(Resource, Resource.id == Chunk.resource_id)
        .join(Topic, Topic.id == Resource.topic_id)
        .join(Course, Course.id == Topic.course_id)
        .where(Course.user_id == user_id, Course.source == source)
        .limit(1)
    ).first() is not None


def enqueue_first_pipeline(session: Session, source: str, user: User) -> Job | None:
    """After a sync: queue a pipeline run over the user's `source` courses
    if none of them has been chunked yet (a new user, or one whose earlier
    run found nothing), so their first quizzes don't wait for the daily
    cron. None when it isn't needed, already queued, or LLM_* is unset."""
    from app.config import settings

    if not settings.llm_api_key or has_chunks(session, user.id, source):
        return None
    return _enqueue_once(session, "pipeline", source, user.email, {})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def reap_stale(session: Session, timeout: timedelta = RUNNING_TIMEOUT) -> dict:
    """Recover jobs whose worker died mid-run. Returns {"reaped": n} plus
    {"failed": m} for those out of attempts, so callers count them as failures."""
    cutoff = _utcnow() - timeout
    stale = session.exec(
        select(Job)
        .where(Job.status == "running", Job.updated_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    out: dict = {}
    for job in stale:
        job.error = f"timed out after {timeout} in running (worker died?)"
        job.status = "failed" if job.attempts >= job.max_attempts else "pending"
        job.available_at = job.updated_at = _utcnow()
        session.add(job)
        if job.status == "failed":
            out["failed"] = out.get("failed", 0) + 1
    session.commit()
    if stale:
        out["reaped"] = len(stale)
    return out


def prune_finished(session: Session, older_than: timedelta) -> int:
    """Delete completed/failed jobs last touched before `older_than` ago.
    Returns how many were deleted."""
    pruned = session.execute(
        delete(Job).where(
            Job.status.in_(("completed", "failed")),
            Job.updated_at < _utcnow() - older_than,
        )
    ).rowcount
    session.commit()
    return pruned


def _claim_next(session: Session) -> Job | None:
    """Atomically move the oldest due pending job to running."""
    now = _utcnow()
    other = aliased(Job)
    # A job is pending-and-due or running at every instant (the claim flips
    # it in one commit), so this can't miss a sync another worker is taking.
    busy = exists().where(
        other.type.not_in(("send_notifications", "pipeline")),
        or_(
            other.status == "running",
            and_(other.status == "pending", other.available_at <= now),
        ),
    )
    job = session.exec(
        select(Job)
        .where(
            Job.status == "pending", Job.available_at <= now,
            or_(Job.type != "send_notifications", ~busy),
        )
        # available_at first: a handed-back job goes behind those already due
        .order_by(Job.available_at, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is not None:
        job.status = "running"
        job.attempts += 1
        job.updated_at = _utcnow()
        session.add(job)
    session.commit()  # releases the row lock; status now keeps others off it
    return job


def _held(job_id, claim: int):
    """WHERE clause matching a job only while this claim still owns it."""
    return (Job.id == job_id) & (Job.status == "running") & (Job.attempts == claim)


class _Heartbeat:
    """Renews a claimed job's lease from a side thread while its handler runs."""

    def __init__(self, bind, job_id, claim: int):
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(bind, job_id, claim), daemon=True
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def _run(self, bind, job_id, claim: int) -> None:
        while not self._stop.wait(HEARTBEAT_EVERY.total_seconds()):
            try:
                with Session(bind) as s:
                    renewed = s.execute(
                        update(Job).where(_held(job_id, claim))
                        .values(updated_at=_utcnow())
                    ).rowcount
                    s.commit()
            except Exception:  # noqa: BLE001 — a missed beat is retried next tick
                continue
            if not renewed:
                return  # reaped; _finish will reject this claim's result


def _finish(session: Session, job_id, claim: int, **values) -> bool:
    """Record the outcome if this claim still owns the job."""
    done = session.execute(
        update(Job).where(_held(job_id, claim))
        .values(**values, updated_at=_utcnow())
    ).rowcount
    session.commit()
    return bool(done)


def run_due(session: Session, limit: int = 5) -> dict:
    """Claim and execute up to `limit` due pending jobs. Returns a summary."""
    global in_handler
    summary: dict = {"completed": 0, "failed": 0, "retried": 0}
    for key, n in reap_stale(session).items():
        summary[key] = summary.get(key, 0) + n
    for _ in range(limit):
        if STOP.is_set():
            break
        job = _claim_next(session)
        if job is None:
            break
        job_id, claim, payload = job.id, job.attempts, dict(job.payload)
        max_attempts = job.max_attempts
        fn = HANDLERS.get(job.type)
        try:
            if fn is None:
                raise ValueError(f"no handler for job type {job.type!r}")
            in_handler = True
            try:
                if STOP.is_set():  # SIGTERM landed during the claim
                    raise Shutdown("stop requested before handler start")
                with _Heartbeat(session.get_bind(), job_id, claim):
                    result = fn(session, payload)
            finally:
                in_handler = False
            outcome = "completed"
            values = {"status": "completed", "error": None,
                      "payload": {**payload, "result": result}}
        except (Shutdown, KeyboardInterrupt):
            # Stopping mid-job: hand it straight back, attempt refunded, so
            # the next worker runs it now rather than after the lease lapses.
            session.rollback()
            _finish(session, job_id, claim, status="pending", attempts=claim - 1,
                    available_at=_utcnow(), error="interrupted by worker shutdown")
            raise
        except Defer as e:
            session.rollback()
            outcome = "deferred"
            values = {"status": "pending", "attempts": claim - 1, "error": str(e)[:2000],
                      "available_at": _utcnow() + e.delay}
        except Exception as e:  # noqa: BLE001 — recorded on the row, never dropped
            session.rollback()  # a DB error inside the handler poisons the txn
            values = {"error": f"{type(e).__name__}: {e}"[:2000]}
            if claim >= max_attempts:
                outcome = "failed"
                values["status"] = "failed"
            else:
                outcome = "retried"
                values["status"] = "pending"  # retry with backoff
                values["available_at"] = _utcnow() + timedelta(seconds=60 * claim)
        if _finish(session, job_id, claim, **values):
            summary[outcome] = summary.get(outcome, 0) + 1
        else:  # lease lapsed and the job was reaped; its new owner reports
            summary["lost"] = summary.get("lost", 0) + 1
    return summary


@handler("sync")
def run_sync_job(session: Session, payload: dict) -> dict:
    """payload: {source, course_id|None, user_email}."""
    from google.auth.exceptions import RefreshError

    from app.auth import find_user, forget_revoked_token, refresh_token_for
    from app.moodle import TokenRejected
    from app.moodle_tokens import decrypt_token, forget_rejected_token
    from app.sync import failed_courses, sync_all, sync_course
    from app.sync_cli import NotConnectedError, build_adapter

    user = find_user(session, payload["user_email"])
    if user is None:
        raise ValueError(f"no such user {payload['user_email']}")
    adapter = build_adapter(payload["source"], user)
    try:
        if payload.get("course_id"):
            stats = sync_course(session, adapter, payload["course_id"], user.id)
            return {payload["course_id"]: stats.as_dict()}
        results = sync_all(session, adapter, user.id)
    except TokenRejected as e:
        # like a revoked Google grant: clear the dead key so Settings asks
        # them to reconnect, instead of failing every daily sync unseen
        own = decrypt_token(user.moodle_token)
        if own is None:
            raise NotConnectedError(
                "Moodle rejected the shared MOODLE_TOKEN; replace it") from e
        forget_rejected_token(session, user, own)
        raise NotConnectedError(
            f"Moodle rejected {user.email}'s key (expired or revoked); "
            "they need to reconnect in Settings") from e
    except RefreshError as e:
        if payload["source"] != "classroom" or "invalid_grant" not in str(e):
            raise
        # the grant is gone; retrying can't help, and a stored dead token
        # would stop the next sign-in from asking for a new one
        own = refresh_token_for(user)
        if own:
            forget_revoked_token(session, user, own)
        raise NotConnectedError(
            f"Google rejected {user.email}'s Classroom access (revoked or "
            "expired); they need to sign in again") from e
    failed = failed_courses(results)
    if results and len(failed) == len(results):
        # nothing synced: fail the job so it retries; a partial sync
        # completes with each failed course's error in the result
        raise RuntimeError(f"every course failed, e.g. {results[failed[0]].error}")
    enqueue_first_pipeline(session, payload["source"], user)
    return {course_id: stats.as_dict() for course_id, stats in results.items()}


@handler("pipeline")
def run_pipeline_job(session: Session, payload: dict) -> dict:
    """payload: {source, user_email}. Extract, chunk and quiz the user's
    `source` courses, paced by LLM_PACE. Waits (Defer) while another run of
    the source, e.g. the pipeline cron, holds its lock. After PIPELINE_SLICE
    it stops between items and hands itself back (Defer(0)) to resume
    behind the other due jobs. Quota running out ends the job early; the
    next cron run resumes where it stopped."""
    import time

    from app.auth import find_user
    from app.config import settings
    from app.pipeline import llm_clients, run_course, single_run

    user = find_user(session, payload["user_email"])
    if user is None:
        raise ValueError(f"no such user {payload['user_email']}")
    source = payload["source"]
    course_ids = session.exec(
        select(Course.id).where(Course.user_id == user.id, Course.source == source)
        .order_by(Course.id)
    ).all()
    chunk_llm, quiz_llm = llm_clients()
    out: dict = {}
    deadline = time.monotonic() + PIPELINE_SLICE.total_seconds()
    with single_run(source, session.get_bind()) as got:
        if not got:
            raise Defer(PIPELINE_BUSY_RETRY, f"another {source} pipeline run is in progress")
        for course_id in course_ids:
            stages = run_course(session, source, course_id, chunk_llm, quiz_llm,
                                pace=settings.llm_pace, deadline=deadline)
            if any(r.out_of_time for r in stages.values()):
                raise Defer(timedelta(0), "time slice used up; resuming after other jobs")
            out[str(course_id)] = {
                name: {**r.counts, **({"quota_exhausted": True} if r.quota_exhausted else {})}
                for name, r in stages.items()
            }
            if any(r.quota_exhausted for r in stages.values()):
                break  # the remaining courses would only hit the same wall
    return out


@handler("send_notifications")
def run_notify_job(session: Session, payload: dict) -> dict:
    from app.config import settings
    from app.notify import check_review_due, send_pending

    users = session.exec(select(User)).all()
    events = sum(
        1 for u in users if check_review_due(session, u.id) is not None
    )
    send = send_pending(
        session, settings.resend_api_key, settings.email_from,
        settings.email_to,
    )
    return {"review_due_events": events, "send": send}
