"""At most one pending/running sync or pipeline job per source and user

Revision ID: 0020
Revises: 0019_quiz_failures
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_active_user_job"
down_revision = "0019_quiz_failures"
branch_labels = None
depends_on = None

# Frozen copies of app.models.ACTIVE_USER_JOB_WHERE and the index's key.
WHERE = "type IN ('sync', 'pipeline') AND status IN ('pending', 'running')"
SOURCE = "(payload ->> 'source')"
USER = "(payload ->> 'user_email')"


def upgrade() -> None:
    # Racing enqueues could leave duplicates. Keep one per group (a running
    # job over a pending one, else the oldest) and fail the rest, rather
    # than let the index creation abort the deploy.
    op.execute(f"""
        UPDATE jobs SET status = 'failed', error = 'superseded duplicate job'
        WHERE {WHERE} AND EXISTS (
            SELECT 1 FROM jobs AS kept
            WHERE kept.type = jobs.type
              AND kept.status IN ('pending', 'running')
              AND (kept.payload ->> 'source') = (jobs.payload ->> 'source')
              AND (kept.payload ->> 'user_email') = (jobs.payload ->> 'user_email')
              AND (kept.status = 'running') >= (jobs.status = 'running')
              AND ((kept.status = 'running') > (jobs.status = 'running')
                   OR (kept.created_at, kept.id) < (jobs.created_at, jobs.id))
        )
    """)
    op.create_index(
        "uq_jobs_active_user_job", "jobs",
        ["type", sa.text(SOURCE), sa.text(USER)], unique=True,
        postgresql_where=sa.text(WHERE), sqlite_where=sa.text(WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_user_job", table_name="jobs")
