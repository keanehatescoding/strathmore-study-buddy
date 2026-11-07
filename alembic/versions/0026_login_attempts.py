"""add login_attempts: password-attempt counts shared by every web instance

Revision ID: 0026
Revises: 0025_outstanding_grants
"""

import sqlalchemy as sa

from alembic import op

revision = "0026_login_attempts"
down_revision = "0025_outstanding_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_attempts_key_at", "login_attempts", ["key", "attempted_at"])
    op.create_index("ix_login_attempts_at", "login_attempts", ["attempted_at"])


def downgrade() -> None:
    op.drop_index("ix_login_attempts_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_key_at", table_name="login_attempts")
    op.drop_table("login_attempts")
