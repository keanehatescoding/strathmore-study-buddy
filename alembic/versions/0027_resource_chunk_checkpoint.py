"""add resumable chunking checkpoint to resources

Revision ID: 0027
Revises: 0026_login_attempts
"""

import sqlalchemy as sa

from alembic import op

revision = "0027_chunk_checkpoint"
down_revision = "0026_login_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resources",
        sa.Column("chunk_checkpoint", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resources", "chunk_checkpoint")