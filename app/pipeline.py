"""Phase 2 pipeline: extract pending resources, then chunk extracted ones.

Usage: python -m app.pipeline --source moodle [--course ID]
       [--extract-only] [--chunk-only]
Chunking needs LLM_* in .env; extraction runs without it.

A resource whose download or chunking fails is deferred with exponential
backoff (Resource.attempts / retry_after, 1h doubling to 24h) rather than
retried on every run; chunking gives up ("failed") after MAX_CHUNK_ATTEMPTS.
Quiz generation backs off the same way per (chunk, attempt) in QuizFailure
and gives up on a chunk after MAX_QUIZ_FAILURES.

Each stage first copies another user's results for the same material
(app.share), counted as "shared", and only then downloads or calls the LLM.
"""

from __future__ import annotations

import argparse
import hashlib
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session, delete, func, or_, select

from app.chunk import FAILED_JOIN, chunk_resource, needs_llm, text_cap_note
from app.config import settings
from app.db import engine
from app.drive import DriveError
from app.extract import ExtractError, SkipResource, cap_text, extract_resource_text
from app.llm import QuotaExhaustedError
from app.models import Chunk, Course, QuizAttempt, QuizFailure, Resource, Topic, User
from app.moodle import ForeignURLError, MoodleError
from app.quiz import generate_for_chunk
from app.share import copy_chunks, copy_extraction, copy_quiz
from app.sync import ContentChanged, commit_if_current


@dataclass
class StageResult:
    """Integer tallies per outcome; a quota stop is a separate flag."""

    counts: Counter[str] = field(default_factory=Counter)
    quota_exhausted: bool = False
    out_of_time: bool = False  # stopped at its deadline; a re-run resumes

    def __str__(self) -> str:
        out = ", ".join(f"{k}={v}" for k, v in self.counts.items())
        if self.quota_exhausted:
            out += " (quota_exhausted: stopped, re-run to resume)"
        if self.out_of_time:
            out += " (out of time: stopped, re-run to resume)"
        return out


def _past(deadline: float | None, result: StageResult) -> bool:
    """Whether a stage should stop before its next item: every item commits
    on its own, so stopping between items loses nothing."""
    if deadline is not None and time.monotonic() >= deadline:
        result.out_of_time = True
    return result.out_of_time


def _scoped(q, course_id, source):
    """Restrict a Resource-joinable query to one course and/or source (None = all).

    The source filter matters for extraction: a Moodle downloader must never
    see a Classroom URL, since it appends the Moodle token to whatever it fetches.
    """
    if source is not None:
        q = q.where(Resource.source == source)
    if course_id is None:
        return q
    return q.join(Topic, Topic.id == Resource.topic_id).where(Topic.course_id == course_id)


MAX_CHUNK_ATTEMPTS = 3
MAX_QUIZ_FAILURES = 3
RETRY_BASE = timedelta(hours=1)
RETRY_CAP = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_at(tries: int) -> datetime:
    """When to try again after `tries` failures in a row: 1h, 2h, 4h ... 24h."""
    return _now() + min(RETRY_BASE * 2 ** (tries - 1), RETRY_CAP)


def _defer(r: Resource, error: str) -> None:
    """Count a failed try and push the next one out."""
    r.attempts = (r.attempts or 0) + 1
    r.retry_after = _retry_at(r.attempts)
    r.error = error[:500]


def _succeeded(r: Resource) -> None:
    r.attempts = 0
    r.retry_after = None


def _due():
    return or_(Resource.retry_after.is_(None), Resource.retry_after <= _now())


def pending_resource_ids(session: Session, course_id=None, source=None) -> list:
    q = (select(Resource.id).where(Resource.status == "pending", _due())
         .order_by(Resource.id))
    return session.exec(_scoped(q, course_id, source)).all()


def chunkable_resource_ids(session: Session, course_id=None, source=None) -> list:
    # ids only: loading every extracted_text up front is what blew memory
    q = (select(Resource.id)
         .where(Resource.extracted_text.is_not(None),
                Resource.status.not_in(["failed", "skipped"]), _due())
         .order_by(Resource.id))
    return session.exec(_scoped(q, course_id, source)).all()


def quiz_chunk_ids(session: Session, course_id=None, source=None,
                   attempt: int | None = None) -> list:
    """Chunk ids in scope; with `attempt`, only those with no QuizAttempt for
    it yet and not backed off or given up after failures, found in one query
    rather than a lookup per chunk."""
    q = (select(Chunk.id).join(Resource, Resource.id == Chunk.resource_id)
         .order_by(Chunk.resource_id, Chunk.order))
    if attempt is not None:
        q = q.where(~select(QuizAttempt.chunk_id).where(
            QuizAttempt.chunk_id == Chunk.id, QuizAttempt.attempt == attempt
        ).exists(), ~select(QuizFailure.chunk_id).where(
            QuizFailure.chunk_id == Chunk.id, QuizFailure.attempt == attempt,
            or_(QuizFailure.failures >= MAX_QUIZ_FAILURES,
                QuizFailure.retry_after > _now()),
        ).exists())
    return session.exec(_scoped(q, course_id, source)).all()


def _owner_downloader_for(session: Session, make):
    """Per-resource downloader acting as the course owner: `make(user)`
    returns one, or None when the owner has no usable token (their file
    resources then stay pending until they connect). Unowned pre-auth
    courses get none: shared tokens belong to their configured owner alone.
    """
    cache: dict = {}

    def for_resource(r: Resource):
        topic = session.get(Topic, r.topic_id)
        course = session.get(Course, topic.course_id) if topic else None
        owner_id = course.user_id if course else None
        if owner_id not in cache:
            user = session.get(User, owner_id) if owner_id else None
            cache[owner_id] = make(user) if user else None
        return cache[owner_id]

    return for_resource


def moodle_downloader_for(session: Session):
    from app.moodle import MoodleClient
    from app.moodle_tokens import token_for

    def make(user):
        token = token_for(user)
        return MoodleClient(settings.moodle_base_url, token).download if token else None

    return _owner_downloader_for(session, make)


def classroom_downloader_for(session: Session):
    """Drive downloads under the owner's Google refresh token (app.drive)."""
    from app.auth import classroom_token_for
    from app.drive import DriveClient, build_service

    def make(user):
        token = classroom_token_for(user)
        if not token:
            return None
        # the client itself: callable as a downloader, and app.share asks its
        # can_read before handing over another user's copy of a Drive file
        return DriveClient(build_service(
            settings.google_client_id, settings.google_client_secret, token))

    return _owner_downloader_for(session, make)


DOWNLOADERS_FOR = {"moodle": moodle_downloader_for, "classroom": classroom_downloader_for}


def _share_failed(session: Session, rid, e: Exception, counts) -> Resource | None:
    """Undo a failed share attempt and reload the resource (None if gone)."""
    session.rollback()
    counts["share_errors"] += 1
    print(f"  share failed on resource {rid}: {type(e).__name__}: {str(e)[:120]}",
          flush=True)
    return session.get(Resource, rid)


def _save_db_failure(session: Session, rid, seen_hash, e: Exception) -> Resource | None:
    """Mark a resource failed after the database refused its extraction (None
    if it is gone or changed meanwhile). Left pending, it would come first,
    and fail the same way, on every later run."""
    session.rollback()
    r = session.get(Resource, rid)
    if r is None or r.content_hash != seen_hash:
        return None
    cause = getattr(e, "orig", None) or e  # the driver's message, not the SQL and its text
    r.status = "failed"
    r.error = f"couldn't save the extracted text: {type(cause).__name__}: {cause}"[:500]
    session.add(r)
    return r if commit_if_current(session, rid, seen_hash) else None


def run_extraction(session: Session, downloader, course_id=None,
                   downloader_for=None, source=None,
                   deadline: float | None = None) -> StageResult:
    result = StageResult(Counter(extracted=0, skipped=0, failed=0))
    counts = result.counts
    ids = pending_resource_ids(session, course_id, source)
    for i, rid in enumerate(ids, 1):
        if _past(deadline, result):
            break
        r = session.get(Resource, rid)
        if r is None:  # retired by a sync since it was listed
            counts["changed"] += 1
            continue
        seen_hash = r.content_hash  # what the text will be of
        dl = downloader_for(r) if downloader_for else downloader
        if dl is None and r.type == "file" and downloader_for:
            # owner hasn't connected this source: leave pending, retry once they do
            counts["no_token"] += 1
            continue
        try:
            shared = copy_extraction(session, r, dl)
        except Exception as e:  # sharing is a shortcut: fall back to downloading
            r = _share_failed(session, rid, e, counts)
            if r is None or r.content_hash != seen_hash:
                counts["changed"] += 1
                continue
            shared = False
        if shared:
            _succeeded(r)
            session.add(r)
            if not commit_if_current(session, rid, seen_hash):
                counts["changed"] += 1
                continue
            counts["shared"] += 1
            print(f"  extract {i}/{len(ids)} shared: {r.title[:60]}", flush=True)
            continue
        try:
            r.extracted_text, note = cap_text(extract_resource_text(r, dl))
            _succeeded(r)
            if r.extracted_text and r.extracted_text.strip():
                r.status = "extracted"
                r.error = note
                outcome = "extracted"
            else:  # nothing to chunk or quiz on; don't bill a chunker call
                r.status = "skipped"
                r.error = "no text extracted"
                outcome = "skipped"
        except SkipResource as e:
            r.status = "skipped"
            r.error = str(e)[:500] or None  # the reason, shown on the resource page
            outcome = "skipped"
        except ForeignURLError as e:
            r.status = "failed"  # not a Moodle file; retrying can't help
            r.error = str(e)[:500]
            outcome = "failed"
        except (MoodleError, DriveError) as e:
            # network blip, rejected token or missing Drive grant: keep
            # pending, retried after a backoff instead of on every run
            _defer(r, str(e))
            outcome = "download_errors"
        except ExtractError as e:
            r.status = "failed"
            r.error = str(e)[:500]
            outcome = "failed"
        except Exception as e:  # parser crash on one bad file must not end the run
            r.status = "failed"
            r.error = f"{type(e).__name__}: {e}"[:500]
            outcome = "failed"
        session.add(r)
        try:
            saved = commit_if_current(session, rid, seen_hash)
        except DBAPIError as e:  # text the database won't store: fail this one, not the run
            r = _save_db_failure(session, rid, seen_hash, e)
            saved, outcome = r is not None, "failed"
        if not saved:
            # a sync replaced the content meanwhile: it is pending again
            counts["changed"] += 1
            print(f"  extract {i}/{len(ids)} changed meanwhile, dropped", flush=True)
            continue
        counts[outcome] += 1
        print(f"  extract {i}/{len(ids)} {r.status}: {r.title[:60]}", flush=True)
    return result


def run_chunking(session: Session, llm, course_id=None, pace: float = 0.0,
                 source=None, deadline: float | None = None) -> StageResult:
    result = StageResult(Counter(chunks=0, cached=0, resources=0))
    counts = result.counts
    ids = chunkable_resource_ids(session, course_id, source)
    for i, rid in enumerate(ids, 1):
        r = session.get(Resource, rid)
        if r is None:  # retired by a sync since it was listed
            counts["changed"] += 1
            continue
        seen_hash = r.content_hash
        has_chunks = session.exec(
            select(func.count()).select_from(Chunk).where(Chunk.resource_id == r.id)
        ).one()
        if has_chunks and r.status == "extracted":
            counts["cached"] += 1
            continue
        # after the cached skip: a slice must reach real work, or each
        # re-run would spend its time re-walking chunked resources
        if _past(deadline, result):
            break
        try:
            shared = copy_chunks(session, r)
        except ContentChanged:
            counts["changed"] += 1
            continue
        except Exception as e:  # sharing is a shortcut: fall back to chunking
            r = _share_failed(session, rid, e, counts)
            if r is None or r.content_hash != seen_hash:
                counts["changed"] += 1
                continue
            shared = 0
        if shared:
            if r.attempts or r.retry_after:
                _succeeded(r)
                session.add(r)
                commit_if_current(session, rid, seen_hash)
            counts["chunks"] += shared
            counts["resources"] += 1
            counts["shared"] += 1
            print(f"  chunk {i}/{len(ids)} shared +{shared}: {r.title[:60]}", flush=True)
            continue
        if llm is None and needs_llm(r):
            counts["needs_llm"] += 1  # left untouched for a run with LLM_* set
            continue
        try:
            n = chunk_resource(session, r, llm)
        except QuotaExhaustedError as e:
            print(f"  quota exhausted, stopping run (resumable): {str(e)[:120]}",
                  flush=True)
            result.quota_exhausted = True
            break
        except ContentChanged:
            # a sync replaced the text mid-call: chunks of the old one dropped,
            # the new content is chunked once extracted
            counts["changed"] += 1
            print(f"  chunk {i}/{len(ids)} changed meanwhile, dropped", flush=True)
            continue
        except Exception as e:
            session.rollback()
            r = session.get(Resource, rid)
            if r is None or r.content_hash != seen_hash:
                counts["changed"] += 1  # failure was on content since replaced
                continue
            # keep the cap note: the retry chunks the same truncated text
            note = text_cap_note(r.error)
            failure = f"{type(e).__name__}: {e}"
            _defer(r, f"{note}{FAILED_JOIN}{failure}" if note else failure)
            if r.attempts >= MAX_CHUNK_ATTEMPTS:
                r.status = "failed"  # existing chunks, if any, are kept
            session.add(r)
            commit_if_current(session, rid, seen_hash)
            counts["errors"] += 1
            print(f"  error on resource {rid} (try {r.attempts}): {str(e)[:120]}",
                  flush=True)
            continue
        if r.attempts or r.retry_after:
            _succeeded(r)
            session.add(r)
            commit_if_current(session, rid, seen_hash)
        counts["chunks"] += n
        print(f"  chunk {i}/{len(ids)} +{n}: {r.title[:60]}", flush=True)
        if n:
            counts["resources"] += 1
            if pace and needs_llm(r):
                session.commit()  # don't sit idle in a transaction while paced
                time.sleep(pace)
    return result


def _quiz_failed(session: Session, chunk_id, attempt: int, e: Exception) -> int:
    """Record a failed generation and back the chunk off; returns the failure
    count (0 when the chunk is gone, e.g. dropped by a resync meanwhile)."""
    if session.get(Chunk, chunk_id) is None:
        return 0
    f = session.get(QuizFailure, (chunk_id, attempt)) or QuizFailure(
        chunk_id=chunk_id, attempt=attempt)
    f.failures += 1
    f.retry_after = _retry_at(f.failures)
    f.error = f"{type(e).__name__}: {e}"[:500]
    session.add(f)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # the chunk's foreign key: deleted since the check
        if session.get(Chunk, chunk_id) is not None:
            raise
        return 0
    return f.failures


def run_quiz(session: Session, llm, course_id=None, attempt: int = 1,
             pace: float = 0.0, source=None, deadline: float | None = None) -> StageResult:
    from uuid import UUID

    from app.notify import course_of_chunk, enqueue_new_material

    result = StageResult(Counter(items=0, chunks=0, skipped=0))
    counts = result.counts
    per_course: Counter[str] = Counter()
    ids = quiz_chunk_ids(session, course_id, source, attempt)
    counts["skipped"] = session.exec(_scoped(
        select(func.count(Chunk.id)).join(Resource, Resource.id == Chunk.resource_id),
        course_id, source,
    )).one() - len(ids)
    for chunk_id in ids:
        if _past(deadline, result):
            break
        chunk = session.get(Chunk, chunk_id)
        if chunk is None:
            counts["changed"] += 1  # dropped by a resync since it was listed
            continue
        called = False
        try:
            items = copy_quiz(session, chunk, attempt)
            shared = items is not None
            if shared:
                counts["shared"] += 1
            else:
                called = True
                items = generate_for_chunk(session, chunk, llm, attempt)
        except QuotaExhaustedError as e:
            print(f"  quota exhausted, stopping run (resumable): {str(e)[:120]}",
                  flush=True)
            result.quota_exhausted = True
            break
        except Exception as e:
            session.rollback()
            tries = _quiz_failed(session, chunk_id, attempt, e)
            counts["errors" if tries else "changed"] += 1
            if tries >= MAX_QUIZ_FAILURES:
                counts["given_up"] += 1
            print(f"  error on chunk {chunk_id} (try {tries}): {str(e)[:120]}",
                  flush=True)
            if pace and called:
                time.sleep(pace)  # a failed call is still a request to pace
            continue
        if session.get(QuizFailure, (chunk_id, attempt)) is not None:
            session.exec(delete(QuizFailure).where(
                QuizFailure.chunk_id == chunk_id, QuizFailure.attempt == attempt))
            session.commit()
        counts["items"] += len(items)
        counts["chunks"] += 1
        print(f"  +{len(items)} items ({counts['chunks']}/{len(ids)} chunks)",
              flush=True)
        if items:
            course = course_of_chunk(session, chunk)
            if course is not None:
                per_course[str(course.id)] += len(items)
        if pace and not shared:
            session.commit()  # don't sit idle in a transaction while paced
            time.sleep(pace)
    for cid, n in per_course.items():
        if enqueue_new_material(session, UUID(cid), n) is not None:
            counts["events"] += 1
    return result


def _lock_key(name: str) -> int:
    digest = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)  # a Postgres bigint


@contextmanager
def advisory_lock(name: str, bind=None, wait: bool = False):
    """Yield whether this process holds the lock `name`. A session-level
    Postgres advisory lock, so it goes away with the process even if it
    dies; SQLite has no concurrent runs to guard against and always gets
    it. `wait` blocks until the lock is free instead of giving up."""
    bind = bind or engine
    if bind.dialect.name != "postgresql":
        yield True
        return
    with bind.connect() as conn:
        key = _lock_key(name)
        if wait:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            got = True
        else:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": key}).scalar()
        conn.commit()
        try:
            yield got
        finally:
            if got:  # pooled connections outlive this: release explicitly
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()


def single_run(source: str, bind=None, wait: bool = False):
    """Yield whether this process holds the run for `source`: two runs over
    the same resources (overlapping cron, a backfill by hand) would both chunk
    and quiz them, duplicating chunks. See advisory_lock."""
    return advisory_lock(f"app.pipeline:{source}", bind, wait)


STAGES = ("extraction", "chunking", "quiz")


def pipeline_job_running(session: Session, source: str) -> bool:
    """Whether the worker is running a queued `pipeline` job for `source`."""
    from app.models import Job

    running = session.exec(
        select(Job).where(Job.type == "pipeline", Job.status == "running")
    ).all()
    return any(j.payload.get("source") == source for j in running)


def llm_clients():
    """(chunk LLM, quiz LLM) from LLM_*; raises LLMError when LLM_API_KEY is unset."""
    from app.llm import LLMClient

    return (
        LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_chunk_model),
        LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_quiz_model),
    )


def run_course(session: Session, source: str, course_id, chunk_llm=None, quiz_llm=None,
               pace: float = 0.0, stages=STAGES,
               deadline: float | None = None) -> dict[str, StageResult]:
    """Run `stages` in order over one course (None = every course of `source`),
    printing each tally as it finishes. The caller holds single_run(source).
    Past `deadline` (a time.monotonic() value) the running stage stops
    between items and later stages are skipped; see StageResult.out_of_time."""
    out: dict[str, StageResult] = {}
    for name in STAGES:
        if name not in stages:
            continue
        if name == "extraction":
            r = run_extraction(session, None, course_id, DOWNLOADERS_FOR[source](session),
                               source=source, deadline=deadline)
        elif name == "chunking":
            r = run_chunking(session, chunk_llm, course_id, pace=pace, source=source,
                             deadline=deadline)
        else:
            r = run_quiz(session, quiz_llm, course_id, pace=pace, source=source,
                         deadline=deadline)
        out[name] = r
        print(f"{name}:", r, flush=True)
        if r.out_of_time:
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["moodle", "classroom"], default="moodle")
    parser.add_argument("--course", default=None)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--chunk-only", action="store_true")
    parser.add_argument("--quiz-only", action="store_true")
    parser.add_argument("--pace", type=float, default=settings.llm_pace,
                        help="seconds between LLM calls (default LLM_PACE; "
                             "raise it for free-tier rate limits)")
    args = parser.parse_args()

    stages = [st for st, skip in zip(STAGES, (
        args.chunk_only or args.quiz_only,
        args.extract_only or args.quiz_only,
        args.extract_only or args.chunk_only,
    )) if not skip]
    chunk_llm = quiz_llm = None
    if "chunking" in stages or "quiz" in stages:
        chunk_llm, quiz_llm = llm_clients()

    for wait in (False, True):
        with single_run(args.source, wait=wait) as got, Session(engine) as session:
            if not got:
                if pipeline_job_running(session, args.source):
                    # a new user's first run (app.jobs): short, so wait it out
                    # rather than skip everyone else's day
                    print(f"a {args.source} pipeline job is running; waiting for it",
                          flush=True)
                    continue
                print(f"another {args.source} pipeline run is in progress; exiting")
                return
            course_ids = [None]
            if args.course:
                # every user enrolled in the course has their own copy of it
                course_ids = session.exec(
                    select(Course.id).where(
                        Course.source == args.source, Course.source_id == args.course,
                        Course.user_id.is_not(None),
                    ).order_by(Course.id)
                ).all()
                if not course_ids:
                    raise SystemExit(f"course {args.course} not synced — run sync_cli first")
            for course_id in course_ids:
                run_course(session, args.source, course_id, chunk_llm, quiz_llm,
                           pace=args.pace, stages=stages)
            return


if __name__ == "__main__":
    main()
