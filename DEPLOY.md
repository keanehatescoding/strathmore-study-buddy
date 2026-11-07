# Deploy

## Google Cloud Run + Supabase (production)

Production runs on Cloud Run in `africa-south1`, against Supabase Postgres.
One image serves everything: the web service and three Cloud Run jobs start
from it with different commands.

1. **Database (Supabase).** Use the **session** pooler (port 5432) or a
   direct connection, never the transaction pooler (port 6543): the pipeline
   takes session-level `pg_advisory_lock`s and psycopg (v3) uses prepared
   statements, and neither survives transaction pooling. Cloud Run has no
   IPv6 egress by default, so the IPv6-only direct host may not connect;
   the session pooler is the safe choice. A bare `postgres://` /
   `postgresql://` URL is switched to the `psycopg` driver automatically.
   The app never uses Supabase's Data API: turn it off, and revoke the
   default privileges Supabase grants `anon` and `authenticated` on
   `public`, so the tables can only be reached through `DATABASE_URL`.
2. **Secrets (Secret Manager),** passed to the service and every job with
   `--set-secrets NAME=NAME:latest`: `DATABASE_URL`, `SECRET_KEY`,
   `APP_BASE_URL` (the public web URL, used in email links and as the
   Google sign-in redirect), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
   (enable both the Classroom API and the Drive API in that Google Cloud
   project, since Classroom materials are Drive files read with
   `drive.readonly`), `LLM_API_KEY`, `LLM_BASE_URL`, `MOODLE_BASE_URL`,
   `RESEND_API_KEY`, `SESSION_SECURE_COOKIE` (`true`). Non-secret settings
   are plain env vars (`--update-env-vars`): `EMAIL_FROM` (a sender on a
   domain verified in Resend; with it empty nothing is sent, and the events
   wait in the queue), `LLM_CHUNK_MODEL` / `LLM_QUIZ_MODEL` /
   `LLM_GRADE_MODEL`, and `LLM_PACE` on the worker job. Every service and
   job needs the same `SECRET_KEY`. In zsh, write `${name}:latest`:
   `$name:latest` applies the `:l` modifier. Everything runs as the
   `study-buddy-web` service account, which needs
   `roles/secretmanager.secretAccessor` on those secrets.
3. **Web service** `strathmore-study-buddy-git`: the image's default
   command (`alembic upgrade head`, then uvicorn on `$PORT`). Cloud Run
   ignores the image's `HEALTHCHECK`; `/health` is still there for uptime
   checks.
4. **Jobs**, each run as `sh -c '<command>'` with no retries
   (`--max-retries 0`; the next scheduled run is the retry):

   | Job | Command | Schedule (UTC) | Task timeout |
   |---|---|---|---|
   | `study-buddy-worker` | `alembic upgrade head && exec python -m app.worker --notify-every 3600` | `*/5 * * * *` | 1 h |
   | `study-buddy-sync` | `alembic upgrade head && python -m app.sync_cli --source moodle --all-users --enqueue && python -m app.sync_cli --source classroom --all-users --enqueue && exec python -m app.worker` | `0 3 * * *` | 2 h |
   | `study-buddy-pipeline` | `alembic upgrade head && exec python -m app.pipeline --source moodle --source classroom --pace 45 --max-minutes 660` | `0 6 * * *` | 12 h |

   Cloud Run has no always-on worker, so `study-buddy-worker` is a single
   pass every 5 minutes instead of `--loop 60`: a sync queued at sign-in
   or by "Sync now", and a new user's first pipeline run (see below), start
   within 5 minutes. A pass that is still draining when the next one starts
   is fine, since job claims use `FOR UPDATE SKIP LOCKED`. The
   notification pass stays hourly: `--notify-every 3600` makes a pass queue
   one only when the last is an hour old. The worker needs `LLM_*`,
   including `LLM_PACE` (`45` on the free Gemini tier, matching the
   pipeline's `--pace`), for that first pipeline run; without
   `LLM_API_KEY` none is queued and new users wait for the daily pipeline.
   The sync job's worker pass has no `--notify-every` on purpose: it always
   runs a notification pass, so the nightly sync's new material goes out
   right away. Events are marked once sent, so the extra pass never emails
   anyone twice. The pipeline gets its own slot after the sync, since paced
   LLM runs take hours and the free Gemini tier rate-limits hard.

   Long-running steps are `exec`'d: until then `sh` is PID 1, which ignores
   the SIGTERM Cloud Run sends on a timeout or cancel, so the step would be
   killed 10 s later without cleaning up. The pipeline job runs both sources
   in one process with `--max-minutes 660`, shared between them (time one
   doesn't need goes to the other), so it stops between items an hour
   before the 12 h timeout, and Classroom gets its turn however long the
   Moodle backlog is. `--pace` overrides `LLM_PACE`, so the pipeline job
   needs no `LLM_PACE` of its own.

   Every service and job starts with `alembic upgrade head`: one running new
   code against an old schema fails (e.g. `column ... does not exist`). It
   is a no-op at head, and concurrent runs serialize on a Postgres advisory
   lock.
5. **Schedules (Cloud Scheduler).** `africa-south1` has no Cloud Scheduler,
   so the three schedulers live in `europe-west1` and call each job's
   `:run` endpoint (`https://run.googleapis.com/v2/projects/PROJECT/locations/africa-south1/jobs/JOB:run`,
   POST, OAuth token) as the `study-buddy-scheduler` service account, which
   has `roles/run.invoker` on each job.
6. **Deploys (Cloud Build).** A trigger in `europe-west1` (Cloud Build
   triggers aren't offered in `africa-south1`) runs `cloudbuild.yaml` on
   every push to `master`. It builds the image with the commit baked in
   (`GIT_COMMIT_SHA`), pushes it to Artifact Registry in `africa-south1`,
   points the web service at it, then moves all three jobs to the same
   image. A job execution already running finishes on its old image. Just
   before deploying, the build checks that `master` still points at its
   commit; if a newer push has landed, it logs "skipping deploy", still
   succeeds, and leaves the deploy to the newer build, so two quick pushes
   can't end with the older one live. The trigger must use the repository's `cloudbuild.yaml` (Configuration →
   Cloud Build configuration file, location `cloudbuild.yaml`), not the
   inline config the Cloud Run setup generated: that one updated only the
   web service and left the jobs on the image they were created with. Its
   service account (the default compute account) needs `roles/run.admin`
   and `roles/iam.serviceAccountUser` on `study-buddy-web`. Builds are listed
   with `gcloud builds list --region europe-west1`.
7. Each user connects their own Moodle account at **Settings** in the nav
   (`/settings/moodle`: sign in once via `login/token.php`, or paste their
   mobile web service key). Tokens are stored encrypted with a key derived
   from `SECRET_KEY`, so rotating `SECRET_KEY` means everyone reconnects.
   The global `MOODLE_TOKEN` is only used for `MOODLE_TOKEN_OWNER` (with no
   owner it is unused), and likewise `GOOGLE_REFRESH_TOKEN` for
   `GOOGLE_REFRESH_TOKEN_OWNER`. Courses synced before sign-in existed stay
   hidden until the owner signs in, or `python -m app.admin_cli claim-unowned
   EMAIL` assigns them (run admin commands as a one-off execution of a job
   with `--args`, or locally against `DATABASE_URL`).
8. Signing in with Google also queues a Classroom sync for that user, and
   the Courses page's "Sync now" button queues one per connected source (at
   most once per 10 minutes per user, counting the nightly syncs too). Users
   who signed in before Drive access was requested must sign in once more;
   until then their Drive files stay pending. Likewise, Classroom
   announcements need `classroom.announcements.readonly`: users who signed
   in before it was requested keep syncing materials and coursework, and
   pick up announcement attachments after their next sign-in. Add
   `https://www.googleapis.com/auth/classroom.announcements.readonly` to the
   consent screen's Data Access list next to the other Classroom scopes. In
   Testing mode that is enough for the listed test users; a published app
   must finish Google's verification for the scope before production use,
   or users see the unverified-app warning.
9. A pipeline run that exhausts the LLM quota stops early
   (`quota_exhausted`) and the next day's run resumes; resources that keep
   failing back off and are eventually marked failed instead of being
   re-billed daily. One resource's cost is bounded too: extracted text is
   truncated to its first 500,000 characters (`MAX_EXTRACTED_CHARS`, about
   a 250-page book), and the chunker sends at most 100 sections
   (`MAX_SECTIONS`). A truncated resource is still studied; its page says
   how much of it was kept. All steps are idempotent; re-running is always
   safe.

Production ran on Railway until 2026-11 (see `docs/incident-2026-10-23.md`);
`scripts/railway-sync-cron-env.sh` and the Railway steps in
`docs/incident-response.md` are from then.

## Single VPS (podman)

```
podman-compose up -d db
.venv/bin/alembic upgrade head
nohup .venv/bin/uvicorn app.main:app --port 8000 &
```

Cron (daily 06:00 sync + notify for every connected user):

```
0 6 * * * cd /srv/study-buddy && .venv/bin/python -m app.sync_cli --source moodle --all-users --enqueue && .venv/bin/python -m app.sync_cli --source classroom --all-users --enqueue && .venv/bin/python -m app.worker
```

Pipeline (daily 09:00, after the sync; paced LLM runs take hours):

```
0 9 * * * cd /srv/study-buddy && .venv/bin/python -m app.pipeline --source moodle --source classroom --pace 45 --max-minutes 1380
```

The pipeline runs from cron (or by hand for a backfill). The one exception
is a user's first sync: when none of their courses for that source has been
chunked yet, the sync job queues a `pipeline` job scoped to their courses
(paced by `LLM_PACE`), so their first quizzes come within the hour rather
than the next morning. It runs in the worker in 10-minute slices
(`PIPELINE_SLICE` in `app/jobs.py`): when a slice is used up it stops
between items and goes to the back of the queue, so other students'
sign-in syncs and the hourly notifications run in between instead of
waiting hours. Notifications don't wait for a pipeline job at all, only
for syncs. On shared course material it mostly copies another user's
results. Only one run per source goes at a time (a Postgres advisory
lock): a cron run that starts while the previous one is still going prints
"another … pipeline run is in progress" and exits 0; one that finds a
`pipeline` job running waits for it instead (best effort: the job status
and the lock are checked separately, so a cron run can still exit while a
job is just starting or deferring; the next run picks the work up); and a
`pipeline` job that finds the lock taken goes back to the queue for 30
minutes. Work
on a resource whose content a sync replaced mid-run is dropped (`changed=` in
the tallies) and redone from the new content.
`--max-minutes` stops a run between items once its time is up
(`out of time` in the tallies, then "time budget used up"); the next run
resumes. Keep it below the next run's start (or the job's timeout), since
an overlapping run for the same source just exits. SIGTERM stops a run
the same way, with exit code 143.
The quiz/chunk runners abort early with `quota_exhausted` when the LLM
quota is gone; re-run the same command later to resume (completed chunks
are skipped via generation keys).

`docker-compose.yml` publishes Postgres on 127.0.0.1 only, since its default
password is public; set `POSTGRES_PASSWORD` (and `POSTGRES_BIND`) before
exposing it anywhere else.

Backups (nightly pg_dump, 14-day retention). Dumps are written `0600` and
only renamed into place once `pg_dump` succeeds; the password is passed via
`PGPASSWORD`, not the command line:

```
0 2 * * * /srv/study-buddy/scripts/backup.sh >> /var/log/study-buddy-backup.log 2>&1
```

## Notes

- Rate limiting is per-process memory (120 POSTs/min default), keyed by
  signed-in user and by client IP for anonymous requests, with at most
  10,000 clients tracked (least recently seen evicted). Behind multiple
  uvicorn workers put a shared limiter or a proxy limit in front.
- Client IPs come from `X-Forwarded-For`, trusted from the peers listed in
  `FORWARDED_ALLOW_IPS` (Dockerfile/Procfile default `*`: any peer). Uvicorn
  takes the leftmost address that isn't a trusted proxy — with `*` that is
  the leftmost entry, which a client can forge unless the proxy **replaces**
  any incoming `X-Forwarded-For` rather than appending to it. So: only let
  the proxy reach uvicorn (never publish the port directly), make the proxy
  overwrite the header (nginx: `proxy_set_header X-Forwarded-For
  $remote_addr;`, not `$proxy_add_x_forwarded_for`), and where the proxy's
  address is known set `FORWARDED_ALLOW_IPS` to it (IPs or CIDRs,
  comma-separated). A forged address only affects the anonymous POST budget
  and logs — signed-in users are rate-limited by account.
- CSP: scripts and `<style>` elements need the per-response nonce
  (`{{ request.state.csp_nonce }}` in templates); no `unsafe-inline`.
  `style="…"` attributes are blocked — add a class to `static/css/app.css`.
- CI (`.github/workflows/ci.yml`) runs on every push/PR: `uv sync --locked`
  (fails if `uv.lock` is stale — run `uv lock` after editing dependencies),
  `ruff check .`, migrations upgrade → `alembic check` → downgrade to base →
  upgrade again on Postgres 16, the full suite on Postgres 16 and on SQLite,
  and a Docker image build.
- The Docker image installs exactly `uv.lock` (no dev tools) and runs as an
  unprivileged user. Its `HEALTHCHECK` polls `/health` on `$PORT`; Cloud Run
  ignores it, but under plain docker/podman start worker and cron containers
  from the image with `--no-healthcheck`, since they serve no HTTP.
- `/health` checks Postgres and returns 503 when unreachable — safe to use
  for platform restart decisions.
- `/version` returns `{"commit": "<sha>"}`, the commit the image was built
  from (`GIT_COMMIT_SHA`, a build arg `cloudbuild.yaml` passes; Railway's
  `RAILWAY_GIT_COMMIT_SHA` is the fallback), and the web and worker
  processes log it at startup ("starting web at commit …"), so a job's
  logs show the commit it ran. After a merge, compare it with
  `git ls-remote origin refs/heads/master` (which asks GitHub directly, so a
  stale local `origin/master` can't mislead) to catch a deploy that didn't
  happen. An image built without the build arg (a plain `docker build`, or
  `gcloud run deploy --source`) reports `"unknown"`.
- Sign-in is limited by `ALLOWED_EMAILS` (default `@strathmore.edu`): a
  comma-separated mix of exact addresses and `@domain` entries. Anyone else
  is sent back to `/login` with an explanation. Set it to your address for
  personal-tool mode, or to an empty value to admit any Google account.
  Removing an address ends that account's existing sessions on its next
  request.
- Sessions last `SESSION_MAX_AGE` seconds without use (default 7 days).
  Logging out ends the session server-side, so a copied cookie stops
  working too; `python -m app.admin_cli revoke-sessions EMAIL` signs an
  account out on every device (e.g. after a lost laptop).
- If the host, `SECRET_KEY` or database may have been compromised, follow
  `docs/incident-response.md`. The one command that cannot wait is
  `python -m app.admin_cli lockdown` (dry run first): it signs everyone out,
  revokes every stored Google grant and forgets every stored Google/Moodle
  token. Run it **before** rotating `SECRET_KEY`, or the stored tokens stop
  decrypting and their grants can only be killed by deleting the OAuth client.
- A student can delete their account under Settings: their Google grant is
  revoked and their keys, courses, questions, review history and queued
  jobs are deleted (other students' copies of the same material stay).
  `python -m app.admin_cli delete-user EMAIL` does the same on request.
  Neither touches `ALLOWED_EMAILS`: signing in again starts an empty account.
- Moodle password sign-in (`/settings/moodle`) is limited to 5 attempts per
  15 minutes per account and per Moodle username, so it can't be used to
  guess another student's password. Like the POST limit, it is per-process.
- Cron observability: set `HEALTHCHECK_PING_URL` (e.g. a healthchecks.io
  check) — the worker pings it after every pass (every minute with
  `--loop 60`) and every 5 minutes while a long pass is still draining a
  backlog, so a period of 15 minutes or so works, and pings `<url>/fail`
  when a job failed for good or the pass itself crashed, so a 6am failure
  pages you instead of showing up as missing quizzes. With `--loop`, a
  crashed pass (e.g. Postgres restarting) is logged and retried next
  interval; the worker keeps running.
- Redeploys: the worker stops cleanly on SIGTERM. A job it was running goes
  straight back to the queue with its attempt refunded, and the new worker
  picks it up on its first pass.
- Completed and failed jobs are deleted after `JOB_RETENTION_DAYS`
  (default 30), so the `jobs` table stays small.
- Web UI requires Google sign-in (`/login`). Generate a real secret:
  `openssl rand -hex 32` → `SECRET_KEY`. Every service refuses to start
  with the public default key unless `DEV=1` is set, which is for local
  development only — never set it on a server. Set `SESSION_SECURE_COOKIE=true`
  behind HTTPS. The Google OAuth client must list `APP_BASE_URL/auth/callback`
  as an authorized redirect: sign-in always sends Google there, whatever
  Host the request came in on, so open the app at that origin.
- Never commit `.env` (gitignored). Rotate Moodle/Google credentials if exposed.
