"""add quiz_failures: per-chunk backoff for failed quiz generation (issue #80)

Revision ID: 0019
Revises: 0018_backfill_quiz_attempts
"""

import sqlalchemy as sa

from alembic import op

revision = "0019_quiz_failures"
down_revision = "0018_backfill_quiz_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quiz_failures",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id", "attempt"),
    )


def downgrade() -> None:
    op.drop_table("quiz_failures")
