"""Background worker: drain the job queue, including one notify pass.

Usage: python -m app.worker [--loop SECONDS] [--notify-every SECONDS]
One pass = reap orphaned jobs, prune old finished ones, enqueue a
send_notifications job (unless one is already queued, or --notify-every
says the last one is too recent), then run due jobs until none are left.
Due syncs are claimed before the notify job, so their new-material events
go out in the same pass. A long drain (a pipeline backlog can take hours)
queues another notify job whenever one falls due and keeps pinging the
healthcheck, so neither waits for the queue to empty.
Schedule with cron (daily) or run --loop for a persistent worker: a short
--loop (60) picks up a sync queued at sign-in within a minute, while
--notify-every (default 3600 with --loop) keeps the notify scan over all
users hourly. In --loop mode a failed pass (e.g. Postgres restarting) is logged and retried next
interval rather than ending the worker. SIGTERM/SIGINT stop it cleanly: a
running job is handed back to the queue with its attempt refunded.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import jobs
from app.config import running_commit, settings
from app.db import engine
from app.jobs import Shutdown, enqueue, prune_finished, reap_stale, run_due
from app.models import Job

log = logging.getLogger("app.worker")

# Within one drain: how often a still-busy worker pings the healthcheck, and
# how often a pass without --notify-every queues another notify job.
PING_EVERY = timedelta(minutes=5)
DRAIN_NOTIFY_EVERY = timedelta(hours=1)


def ping_healthcheck(fail: bool = False) -> None:
    """Notify a dead-man's-switch monitor (e.g. healthchecks.io): the plain
    URL on success, URL + "/fail" when jobs failed or the pass crashed, so
    failures alert immediately. Monitoring must never fail the run."""
    url = settings.healthcheck_ping_url
    if not url:
        return
    if fail:
        url = url.rstrip("/") + "/fail"
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception:
        pass


def _notify_due(session: Session, every: timedelta | None) -> bool:
    """Whether this pass should queue a notify job: always without `every`,
    else only when the newest one (in any state) is at least `every` old."""
    if not every:
        return True
    last = session.exec(
        select(func.max(Job.created_at)).where(Job.type == "send_notifications")
    ).one()
    if last is None:
        return True
    if last.tzinfo is None:  # SQLite drops the zone
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= every


def _queue_notify(session: Session) -> None:
    try:
        enqueue(session, "send_notifications", max_attempts=1)
    except IntegrityError:  # uq_jobs_active_notify: one is already queued
        session.rollback()


def _drain(notify_every: timedelta | None = None) -> dict:
    totals: dict = {"completed": 0, "failed": 0, "retried": 0}
    with Session(engine) as session:
        # Reap first: a notify job orphaned by a crashed worker would
        # otherwise block this pass's enqueue and then be failed unrun.
        for key, n in reap_stale(session).items():
            totals[key] = totals.get(key, 0) + n
        pruned = prune_finished(session, timedelta(days=settings.job_retention_days))
        if pruned:
            totals["pruned"] = pruned
        if _notify_due(session, notify_every):
            _queue_notify(session)
        pinged = time.monotonic()
        while not jobs.STOP.is_set():  # drain; failed jobs back off, so this terminates
            batch = run_due(session)
            for key, n in batch.items():
                totals[key] = totals.get(key, 0) + n
            if not any(batch.values()):
                break
            # Still busy: don't make the notify job or the monitor wait for
            # the whole backlog.
            if _notify_due(session, notify_every or DRAIN_NOTIFY_EVERY):
                _queue_notify(session)
            if time.monotonic() - pinged >= PING_EVERY.total_seconds():
                ping_healthcheck(fail=totals["failed"] > 0)
                pinged = time.monotonic()
    return totals


def run_once(notify_every: timedelta | None = None) -> dict:
    try:
        totals = _drain(notify_every)
    except Exception:
        ping_healthcheck(fail=True)
        raise
    ping_healthcheck(fail=totals["failed"] > 0)
    return totals


def _request_stop(signum, frame) -> None:
    jobs.STOP.set()
    if jobs.in_handler:  # interrupt the job now; run_due hands it back
        raise Shutdown(signal.Signals(signum).name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                        help="repeat every N seconds (0 = single pass)")
    parser.add_argument("--notify-every", type=int, default=None,
                        help="queue the notify job at most every N seconds "
                             "(default: 3600 with --loop, every pass without)")
    args = parser.parse_args()
    notify_secs = args.notify_every
    if notify_secs is None:
        notify_secs = 3600 if args.loop else 0
    notify_every = timedelta(seconds=notify_secs) if notify_secs else None
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("starting worker at commit %s", running_commit())
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    try:
        while not jobs.STOP.is_set():
            try:
                totals = run_once(notify_every)
                if not args.loop or any(totals.values()):  # quiet idle polls
                    print(totals, flush=True)
            except Exception:
                if not args.loop:
                    raise  # a one-off (cron) run should exit non-zero
                log.exception("worker pass failed; retrying in %ss", args.loop)
            if not args.loop or jobs.STOP.wait(args.loop):
                break
    except Shutdown:
        pass  # the interrupted job is already back in the queue
    if jobs.STOP.is_set():
        log.info("worker stopped on signal")


if __name__ == "__main__":
    main()
