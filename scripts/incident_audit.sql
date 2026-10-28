-- Incident audit for the Study Buddy Postgres database.
--
-- Read-only. Run with psql as the database owner (Railway: `railway connect
-- Postgres`, or `psql "service=incident"`; keep the password out of the
-- command line, see docs/incident-response.md), then save the output into a
-- new evidence directory outside the repo, never over an earlier file, and
-- keep stderr too (a failed query is part of the evidence):
--
--   psql -X --set=ON_ERROR_STOP=on "service=incident" -f scripts/incident_audit.sql \
--     > "$IR/db-audit.txt" 2> "$IR/db-audit.err"
--
-- Set `since` to the earliest moment the platform may have been compromised,
-- before running. Everything after it is worth explaining.
--
-- This proves configuration, objects and row counts. It cannot prove which
-- rows were *read*: only server/statement logs (log_statement=all) or the
-- platform's audit log can show that. Do not treat a clean result here as
-- "no data was accessed".

\set ON_ERROR_STOP on

\set since '2026-10-17 00:00:00+03'
\pset pager off
\timing off

\echo '== 1. context ========================================================'
SELECT current_database() AS database, current_user AS connected_as,
       inet_server_addr() AS server_addr, now() AS at;
SELECT version();

\echo '== 2. roles (a new login role or SUPERUSER is a backdoor) ============'
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin,
       rolreplication, rolbypassrls, rolvaliduntil, rolconnlimit
FROM pg_roles
WHERE rolname NOT LIKE 'pg\_%'
ORDER BY rolsuper DESC, rolcanlogin DESC, rolname;

SELECT member.rolname AS member, granted.rolname AS granted_role, m.admin_option
FROM pg_auth_members m
JOIN pg_roles member ON member.oid = m.member
JOIN pg_roles granted ON granted.oid = m.roleid
WHERE member.rolname NOT LIKE 'pg\_%'
ORDER BY 1, 2;

-- Per-role/database settings can hijack search_path into an attacker schema.
SELECT role.rolname, db.datname, s.setconfig
FROM pg_db_role_setting s
LEFT JOIN pg_roles role ON role.oid = s.setrole
LEFT JOIN pg_database db ON db.oid = s.setdatabase
ORDER BY 1, 2;

\echo '== 3. schemas and objects, with their owners =========================='
SELECT nspname, pg_get_userbyid(nspowner) AS owner
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
ORDER BY 1;

SELECT n.nspname AS schema, c.relname, c.relkind,
       pg_get_userbyid(c.relowner) AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
ORDER BY owner, 1, 2;

\echo '== 4. persistence: event triggers, triggers, rules ==================='
SELECT evtname, evtevent, evtowner::regrole AS owner, evtfoid::regproc AS func,
       evtenabled
FROM pg_event_trigger;

SELECT n.nspname AS schema, c.relname AS table, t.tgname,
       pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal
ORDER BY 1, 2, 3;

SELECT n.nspname AS schema, c.relname AS table, r.rulename,
       pg_get_ruledef(r.oid) AS definition
FROM pg_rewrite r
JOIN pg_class c ON c.oid = r.ev_class
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE r.rulename <> '_RETURN'
ORDER BY 1, 2, 3;

\echo '== 5. extensions, foreign servers, public functions =================='
SELECT e.extname, e.extversion, n.nspname AS schema
FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
ORDER BY 1;

SELECT srvname, srvtype, srvversion, srvoptions FROM pg_foreign_server;
SELECT fdwname FROM pg_foreign_data_wrapper;

SELECT n.nspname AS schema, p.proname, l.lanname,
       pg_get_userbyid(p.proowner) AS owner, p.prosecdef AS security_definer
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1, 2;

-- If pg_cron is installed (see section 5), it can hold scheduled attacker SQL:
-- SELECT * FROM cron.job;

\echo '== 6. grants to unexpected roles ====================================='
SELECT grantee, table_schema, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'public' AND grantee <> 'PUBLIC'
ORDER BY 1, 2, 3;

\echo '== 7. live connections and logging settings =========================='
SELECT pid, usename, application_name, client_addr, state,
       backend_start, query_start, left(query, 120) AS query
FROM pg_stat_activity
WHERE backend_type = 'client backend'
ORDER BY backend_start;

SHOW listen_addresses;
SHOW ssl;
SHOW log_statement;
SHOW log_connections;
SHOW log_disconnections;
SHOW shared_preload_libraries;

\echo '== 8. blast radius: how many stored credentials? ====================='
SELECT count(*) AS users,
       count(google_refresh_token) AS google_refresh_tokens,
       count(moodle_token) AS moodle_tokens,
       count(*) FILTER (WHERE created_at >= :'since') AS users_created_since
FROM users;

\echo '== 9. accounts created or changed in the window ======================'
SELECT id, email, created_at, session_version, notify_email,
       google_refresh_token IS NOT NULL AS has_google_token,
       moodle_token IS NOT NULL AS has_moodle_token
FROM users
WHERE created_at >= :'since'
ORDER BY created_at;

-- Domains should all be your ALLOWED_EMAILS (normally just strathmore.edu).
SELECT split_part(email, '@', 2) AS domain, count(*) AS users
FROM users
GROUP BY 1
ORDER BY 2 DESC;

\echo '== 10. job queue (attacker-queued work would appear here) ============'
SELECT type, status, count(*) AS jobs
FROM jobs
GROUP BY 1, 2
ORDER BY 1, 2;

SELECT id, type, status, attempts, created_at, updated_at,
       left(payload::text, 200) AS payload
FROM jobs
ORDER BY created_at DESC
LIMIT 50;

\echo '== 11. write activity (cumulative since stats reset) ================='
SELECT relname, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup, n_dead_tup
FROM pg_stat_user_tables
ORDER BY n_tup_del DESC, n_tup_upd DESC, n_tup_ins DESC
LIMIT 25;

\echo '== 12. large objects (a common staging ground for an export) ========='
SELECT count(*) AS large_objects, coalesce(sum(length(data)), 0) AS bytes
FROM pg_largeobject;

\echo '== 13. optional: statement statistics, if pg_stat_statements is on ==='
-- SELECT calls, rows, left(query, 120) AS query
-- FROM pg_stat_statements
-- ORDER BY rows DESC
-- LIMIT 30;

\echo '== done. Explain every unexpected row before closing the incident. ==='
