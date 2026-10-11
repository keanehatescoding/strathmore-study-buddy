# Strathmore Study Buddy

A web app that pulls course material from Strathmore's Moodle (eLearning) and
Google Classroom, and quizzes you on it with spaced repetition.

## How it works

```
Moodle / Classroom → sync → extract text → LLM chunking → quiz generation
    → review queue (SM-2) → email notifications
```

- **Ingestion**: Moodle web-services API (token auth) and Google Classroom
  (OAuth2), diff-based sync — new resources, changed content re-queued,
  unchanged skipped by SHA256.
- **Extraction**: PDF, PPTX, DOCX, legacy PPT/DOC and RTF, page text;
  YouTube transcripts where available. Unusable content is marked `skipped`, never silently stuck.
- **Chunking**: LLM semantic chunking into single-concept chunks with
  source offsets for a future "show source" UI.
- **Quizzes**: 2–4 grounded items per chunk (MCQ + short-answer), difficulty
  tagged, idempotent generation keys.
- **Review**: SM-2 scheduling, instant MCQ grading, LLM short-answer grading
  against key points, streak/accuracy stats.
- **Notifications**: batched new-material and threshold-gated review-due
  emails via Resend, sent by a background worker.

## Quickstart

Requirements: Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), podman or docker.

```bash
uv sync                # .venv from uv.lock, dev tools (pytest, ruff) included
cp .env.example .env   # fill in MOODLE_TOKEN etc.; uncomment DEV=1 (or set SECRET_KEY)
podman-compose up -d db
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8000
```

Sync one course, then run the pipeline (replace `YOU@X` with your login email):

```bash
.venv/bin/python -m app.sync_cli --source moodle --user YOU@X --course 7034
.venv/bin/python -m app.pipeline --source moodle --course 7034
.venv/bin/python -m app.worker   # send queued notifications
```

Open http://localhost:8000 — sign in with Google, then browse courses →
topics → resources/chunks, plus `/review` (due queue) and `/stats`.

## Configuration (.env)

| Key | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection |
| `MOODLE_BASE_URL` | Moodle site; users connect their own account at `/settings/moodle` |
| `MOODLE_TOKEN`, `MOODLE_TOKEN_OWNER` | Optional fallback token, used only for that owner's email (empty owner = unused) |
| `GOOGLE_CLIENT_ID/SECRET` | Classroom + Drive (read-only) OAuth (Web app, Testing mode; enable both APIs) |
| `GOOGLE_REFRESH_TOKEN`, `GOOGLE_REFRESH_TOKEN_OWNER` | Optional fallback Classroom token, used only for that owner's email |
| `LLM_BASE_URL`, `LLM_API_KEY` | OpenAI-compatible endpoint |
| `LLM_CHUNK_MODEL`, `LLM_QUIZ_MODEL`, `LLM_GRADE_MODEL` | Per-stage models (cheap → quality) |
| `LLM_PACE` | Seconds between pipeline LLM calls (default 0; `--pace` overrides) |
| `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_TO` | Notifications (Resend free tier) |
| `APP_BASE_URL` | Public origin for email links and the Google sign-in redirect `APP_BASE_URL/auth/callback` (default `http://localhost:8000`) |
| `TIMEZONE` | IANA zone for study days: streaks start at local midnight (default `Africa/Nairobi`) |
| `SECRET_KEY` | Signs sessions and encrypts stored tokens (`openssl rand -hex 32`); every service needs the same value |
| `DEV` | Local development only: accepts the public default `SECRET_KEY` (never on a server) |
| `SESSION_SECURE_COOKIE` | `true` behind HTTPS so the session cookie is never sent over plain HTTP |
| `SESSION_MAX_AGE` | Seconds a sign-in lasts without use (default 7 days) |
| `ALLOWED_EMAILS` | Who may sign in: addresses and/or `@domain` entries, comma-separated (default `@strathmore.edu`; empty = any Google account) |
| `FORWARDED_ALLOW_IPS` | Proxies trusted for `X-Forwarded-For`/`-Proto`, IPs or CIDRs (default `*`; see DEPLOY.md) |
| `HEALTHCHECK_PING_URL` | Dead-man's switch: the worker pings it after each pass, `<url>/fail` on failure (empty = off) |
| `JOB_RETENTION_DAYS` | Days finished jobs are kept before the worker deletes them (default 30) |
| `LLM_GRADE_TIMEOUT`, `LLM_GRADE_ATTEMPTS` | Short-answer grading budget inside a web request (default 30 s, 2 tries) |

## Layout

- `app/sync.py`, `app/moodle.py`, `app/classroom.py` — ingestion
- `app/extract.py`, `app/chunk.py`, `app/quiz.py` — learning pipeline
- `app/grade.py`, `app/srs.py`, `app/stats.py` — review + scheduling
- `app/notify.py`, `app/worker.py`, `app/jobs.py` — notifications + job queue
- `app/auth.py`, `app/security.py` — Google sign-in, CSRF, rate limiting, headers
- `app/main.py`, `templates/` — server-rendered UI
- `alembic/versions/` — migrations (canonical schema)

`.venv/bin/python -m pytest` runs the suite on in-memory SQLite, no network.
With `TEST_DATABASE_URL` pointing at a **scratch** Postgres database it runs
on Postgres instead, wiping that database's `public` schema before every
test — CI runs both, plus `ruff check .` and a full migration
upgrade → downgrade → upgrade round-trip.
See `DEPLOY.md` for Railway/VPS deployment.
