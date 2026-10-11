"""retry .ppt/.doc files that failed before they could be read (issue #122)

Revision ID: 0024
Revises: 0023_item_flags
"""

from alembic import op

revision = "0024_retry_legacy_office"
down_revision = "0023_item_flags"
branch_labels = None
depends_on = None

# the errors app.extract.extract_bytes wrote for them: by mime type, or by
# file name where the server sent a generic one
UNSUPPORTED = (
    "unsupported type (mime=application/vnd.ms-powerpoint,%",
    "unsupported type (mime=application/msword,%",
    "unsupported type (mime=application/rtf,%",
    "unsupported type (%.ppt)",
    "unsupported type (%.pps)",
    "unsupported type (%.doc)",
    "unsupported type (%.rtf)",
)


def upgrade() -> None:
    matches = " OR ".join(f"lower(error) LIKE '{pattern}'" for pattern in UNSUPPORTED)
    op.execute(
        "UPDATE resources SET status = 'pending', error = NULL, attempts = 0, "
        "retry_after = NULL "
        f"WHERE type = 'file' AND status = 'failed' AND ({matches})"
    )


def downgrade() -> None:
    pass  # pending rows just get extracted (or failed with a real error) next run
