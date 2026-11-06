"""Store encrypted DTB credentials per application token."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "personal_dtb_tokens",
        sa.Column("owner_id", sa.String(), primary_key=True),
        sa.Column("encrypted_token", sa.String(), nullable=False),
    )


def downgrade():
    op.drop_table("personal_dtb_tokens")
