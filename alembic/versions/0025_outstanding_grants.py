"""add outstanding_grants: grants a lockdown could not prove dead

Revision ID: 0025
Revises: 0024_retry_legacy_office
"""

import sqlalchemy as sa

from alembic import op

revision = "0025_outstanding_grants"
down_revision = "0024_retry_legacy_office"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outstanding_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "email", name="uq_outstanding_grants_provider_email"),
    )


def downgrade() -> None:
    op.drop_table("outstanding_grants")
