"""add item_flags: skipped and suspended questions (issue #116)

Revision ID: 0023
Revises: 0022_course_archived
"""

import sqlalchemy as sa

from alembic import op

revision = "0023_item_flags"
down_revision = "0022_course_archived"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_flags",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("quiz_item_id", sa.Uuid(), nullable=False),
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["quiz_item_id"], ["quiz_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "quiz_item_id"),
    )
    op.create_index("ix_item_flags_quiz_item_id", "item_flags", ["quiz_item_id"])


def downgrade() -> None:
    op.drop_index("ix_item_flags_quiz_item_id", table_name="item_flags")
    op.drop_table("item_flags")
