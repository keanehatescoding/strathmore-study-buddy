"""courses.archived: hide old-semester courses (issue #115)

Revision ID: 0022
Revises: 0021_notify_attempts
"""

import sqlalchemy as sa

from alembic import op

revision = "0022_course_archived"
down_revision = "0021_notify_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("courses", sa.Column(
        "archived", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("courses", "archived")
