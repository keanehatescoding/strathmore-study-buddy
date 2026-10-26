# Incident response: leaked platform, `SECRET_KEY` or database

Use this when Railway (or any host holding this app's environment), the
database, or `SECRET_KEY` may have been accessed by someone else. It is
written for this app's actual blast radius, not as generic advice.

The order matters more than the speed: **`lockdown` must run before
`SECRET_KEY` is rotated**, or the stored Google tokens stop decrypting and
their grants can no longer be revoked from here.

---

## 1. What an attacker with platform access has

| Asset | Consequence |
|---|---|
| `SECRET_KEY` | Forges the signed session cookie (`SessionMiddleware`) for **any user**, with no password. Also derives the Fernet keys for stored Moodle/Google tokens (purpose-separated, same secret) and the unsubscribe links. |
| `DATABASE_URL` | All users (emails), courses, resources, assignments, quiz/review history, notifications, job queue, and the encrypted tokens above. |
| `GOOGLE_CLIENT_ID/SECRET`, stored Google refresh tokens | Sign-in plus Classroom and `drive.readonly` access to each connected student's Google account. Rotating only the client secret does **not** revoke tokens already issued. |
| `MOODLE_TOKEN` and per-user Moodle tokens | Read access to Moodle courses and files for the token's owner. |
| `LLM_API_KEY`, `RESEND_API_KEY` | Billing abuse; phishing from the verified sending domain. |
| Deploy rights on `web`, `worker`, `sync-cron`, `pipeline-cron` | Arbitrary code with env + DB access, persistence, exfiltration, `railway up` outside GitHub. |

`SECRET_KEY` gives impersonation; the tokens give third-party access. Rotate
both, and treat every stored credential as already plaintext.

## 2. Preserve evidence first

Deleting a compromised service or database destroys the only record of what
happened. Capture, in this order:

- [ ] Railway: workspace/project audit log (if the plan exposes it), deployment
      history with source and commit, build logs, service list, custom domains,
      volumes, env-var **names**, team members and tokens. Screenshot anything
      you cannot export. `railway status` output too.
- [ ] Which code is actually running: `curl -s https://<app-host>/version`
      returns the deployed commit. Compare with `git rev-parse origin/master`.
      A mismatch means someone deployed with `railway up`; keep that image/log.
- [ ] Database snapshot: `pg_dump --no-owner "$DATABASE_URL" > incident.dump`.
      If the Railway database is private-only, `pg_dump` from your machine
      will fail to connect: use the Railway backup instead, and do not open
      a public TCP proxy just to dump it. Do this before any rotation.
- [ ] DB audit: `psql "$DATABASE_URL" -f scripts/incident_audit.sql >
      incident-db-audit.txt` (edit `since` in the file first). If the
      database is private-only, run `railway connect Postgres` (as the
      header of `scripts/incident_audit.sql` says), then inside psql:
      `\o incident-db-audit.txt` and `\i scripts/incident_audit.sql`, so the
      output is saved with the rest of the evidence.
- [ ] Mail log from Resend (sends you did not trigger), LLM usage/billing,
      Google Cloud audit log for the OAuth client (Workspace:
      admin.google.com → Reports → Audit log → OAuth log events).
- [ ] Put all of it somewhere the attacker cannot delete, outside the project.

## 3. Contain the platform

- [ ] Railway: change the password, enable MFA, **revoke every account and
      project token**, remove unknown members/invites, check for new services
      (miners), custom domains, volumes and integrations.
- [ ] If the GitHub repo is linked: audit collaborators, deploy keys, SSH keys
      (`gh api user/keys`, `gh api repos/:owner/:repo/keys`), Actions secrets,
      and recent pushes. Keep branch protection on `master`.
- [ ] Local machine: `railway logout`, delete
      `~/.railway/sessions/*.session` and any project token in shell history,
      then log in again with MFA. Rotate any GitHub token used from it.
- [ ] Put `web` into maintenance/offline once the snapshot exists, and keep
      it offline until 4e. Forged sessions stay valid until 4b, and the other
      credentials in 4c/4d stay usable until you have rotated them.

## 4. Rotate, in this order

### 4a. Application lockdown (before touching `SECRET_KEY`)

```sh
# from the repo root, with the app's dependencies installed (`uv sync`)
# preview first
railway run --service web --environment production \
  python -m app.admin_cli lockdown --dry-run

# then do it: signs everyone out, revokes each Google grant with Google,
# forgets every stored Google and Moodle token
railway run --service web --environment production \
  python -m app.admin_cli lockdown
```

(`railway ssh` into the web service and run the same command there if
`railway run` is unavailable.)

Keep `web` offline; do not redeploy it yet. `lockdown` only bumps
`session_version`, which is a small integer stored in the database and
carried in the cookie, so a holder of the leaked `SECRET_KEY` can sign a
cookie with the new version and it is accepted until 4b. Lockdown stops old
cookies, not forged ones. (`lockdown` runs as a one-off command and does not
need `web` online.) Bring `web` back only at 4e, after every secret is
rotated.

`--keep-moodle` leaves Moodle tokens stored; `--no-google-revoke` clears
without calling Google (only if Google is unreachable — delete the OAuth
client instead, below).

`lockdown` exits **1** until every grant it could not prove dead is
accounted for, **including on a retry**: the tokens are gone after the first
run, so what it could not revoke is recorded (provider, email, reason; no
secrets) in the `outstanding_grants` table and the exit code reads that.

| Left live | Recorded as | Cleared by |
|---|---|---|
| Google grant Google could not be asked about or did not confirm (unreachable, `--no-google-revoke`, token no longer decrypts) | `google` | Delete the OAuth client at Google (4c) or revoke the grants, then re-run with `--google-revoked` |
| Any per-user Moodle token (forgotten here, or kept with `--keep-moodle`): the app cannot revoke them | `moodle` | Delete them in Moodle (Site administration → Server → Web services → Manage tokens), then re-run with `--moodle-revoked` |
| Shared `GOOGLE_REFRESH_TOKEN` not revoked / shared `MOODLE_TOKEN` | nothing: the variable is still set on every run | First replace or unset the variable on every service **and** finish the provider side (revoke the grant at Google; delete the token in Moodle). Only then pass `--google-revoked` / `--moodle-revoked`: the flag is accepted on your word and does not unset or revoke anything |

A grant Google confirms revoked is never recorded. The two flags are your
word that the grants were removed outside the app, so pass them only after
that cleanup is actually done: they clear every record
of that provider and the run prints how many it took on trust. Each flag
only covers its own provider. The records are not tied to the user row, so
deleting an account does not erase them. `--dry-run` writes nothing and
never fails, but lists records left by earlier runs.

### 4b. `SECRET_KEY`

```sh
openssl rand -hex 32
```

Set the new value on **every** service (web, worker, sync-cron,
pipeline-cron — they must match; `scripts/railway-sync-cron-env.sh`). This
invalidates every session (including any forged with the old key),
unsubscribe link, and remaining encrypted token. Keep `web` offline: the
credentials in 4c and 4d are still usable until rotated. It comes back at 4e.
Users will sign in again and reconnect Moodle under Settings.

### 4c. Google OAuth

Revoking the client **secret** alone leaves existing refresh tokens alive.
Do one of:

- `lockdown` already called `/revoke` per token (check its report: it lists
  each grant it could not revoke, and keeps exiting 1 until you pass
  `--google-revoked`), **and/or**
- delete the OAuth client in Google Cloud → APIs & Services → Credentials,
  which invalidates every token issued to it (strongest), then create a new
  client, update the redirect URI (`APP_BASE_URL/auth/callback`), and update
  `GOOGLE_CLIENT_ID/SECRET` everywhere.

Users re-consent on next sign-in. Clear the old-client grants for anyone who
does not return: myaccount.google.com/permissions.

### 4d. Everything else

| Secret | Action | User impact |
|---|---|---|
| `DATABASE_URL` | Check `pg_roles` for new roles first. Set the new password with psql's `\password <user>` (hashes client-side; a literal `ALTER ROLE ... PASSWORD '...'` leaves the plaintext in psql history and server logs). Update `DATABASE_URL` on every service (`web` stays offline). **Then end the existing sessions**, because a password change does not close connections already authenticated: from another admin role, `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '<user>' AND pid <> pg_backend_pid();`. If the provider does not let you terminate backends, isolate the database (remove the public TCP proxy, restart the Postgres service) and confirm with `pg_stat_activity` that only your services are connected. | None if cut over cleanly. |
| `MOODLE_TOKEN` (global) | Delete the token in Moodle (Site administration → Server → Web services → Manage tokens). | Only `MOODLE_TOKEN_OWNER`. |
| Per-user Moodle tokens | Delete them in the same Moodle token list. `lockdown` only forgets our copy; the tokens stay valid at Moodle until removed there, and it keeps exiting 1 until you confirm with `--moodle-revoked`. | Users reconnect at Settings → Moodle. |
| `RESEND_API_KEY` | Revoke and recreate; check sent mail for phishing; review domain DNS/DKIM. | None. |
| `LLM_API_KEY` | Revoke and recreate; check usage/billing. | None. |
| `HEALTHCHECK_PING_URL` | Recreate the check URL if it appeared in logs. | None. |
| GitHub token/SSH key | Revoke and reissue; review audit log for pushes. | None. |

### 4e. Deploy clean

Only after every secret above is changed (including 4d: `DATABASE_URL` with
old sessions terminated, `MOODLE_TOKEN`, `RESEND_API_KEY`, `LLM_API_KEY`),
redeploy **all** services together from a known-good commit and bring `web`
back online. Verify
`/version` matches `git rev-parse origin/master` and that no service has
extra start commands or variables you did not set.

## 5. Scope the exposure

- [ ] `scripts/incident_audit.sql`: new roles, triggers/rules/event triggers,
      extensions (`dblink`, `pg_cron`), foreign servers, grants, live sessions,
      per-role `search_path` settings, users created in the window, unusual
      email domains, unexpected jobs, large objects.
- [ ] Compare the deployed commit to `master`; if it differs, treat the
      deployment as malicious code and read its diff.
- [ ] Database logs / `log_statement` output if enabled. The audit script
      cannot prove which rows were read.
- [ ] Decide, from the evidence: precautionary, or confirmed access to
      personal data. Record the decision and why.

## 6. Notify

If personal data was or may have been accessed, this is a personal-data
breach under Kenya's Data Protection Act 2019:

- notify the ODPC within **72 hours** of becoming aware (s.43), and
- notify affected data subjects where the breach is likely to result in
  **high risk** to their rights and freedoms.

Drafts: `docs/incident-2026-10-23-notices.md`. Send from a domain you still
control; if Resend was exposed, send through another provider or the
university's mail.

## 7. Recover and verify

- [ ] Sign in as a normal user: forced re-login, Google consent, Moodle
      reconnect, one sync, one quiz.
- [ ] Confirm an old session cookie is rejected (browser with a saved cookie
      gets redirected to `/login`).
- [ ] Confirm `/version` is the clean commit and the worker is draining jobs.
- [ ] Watch error rates, auth failures and Resend/LLM usage for 24–48 h.

## 8. Afterwards

- [ ] Rotate secrets on a schedule, not only after incidents; keep them in one
      place per service (`scripts/railway-sync-cron-env.sh`).
- [ ] Turn on Railway/GitHub MFA for every collaborator; least-privilege
      tokens; branch protection.
- [ ] Enable Postgres statement logging or a managed audit trail if available.
- [ ] Add the incident record and its evidence index to `docs/` before closing.
- [ ] Write down what would have detected this sooner (billing alert, deploy
      notification, login alert) and turn it on.

---

## Do not

- Rotate `SECRET_KEY` before running `lockdown` — the Google grants become
  unrevokable from the app.
- Delete the database, service, or logs before snapshotting.
- Rotate the Google client secret and assume existing refresh tokens died.
- Assume a clean GitHub history means a clean deployment (`railway up`).
- Reuse any secret that appeared in the platform's env or logs.
