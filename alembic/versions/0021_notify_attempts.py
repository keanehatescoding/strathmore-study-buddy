"""notification_events.attempts: cap delivery retries (issue #112)

Revision ID: 0021
Revises: 0020_active_user_job
"""

import sqlalchemy as sa

from alembic import op

revision = "0021_notify_attempts"
down_revision = "0020_active_user_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_events", sa.Column(
        "attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("notification_events", "attempts")
