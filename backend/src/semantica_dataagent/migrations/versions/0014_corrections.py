"""Persist correction proposals and metadata overrides."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "correction_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String()),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("patch", sa.Text(), nullable=False),
        sa.Column("original", sa.Text(), nullable=False),
        sa.Column("diff", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reviewer_id", sa.String()),
        sa.Column("reviewed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "metadata_overrides",
        sa.Column("target", sa.String(), primary_key=True),
        sa.Column("comment", sa.Text(), nullable=False),
    )


def downgrade():
    op.drop_table("metadata_overrides")
    op.drop_table("correction_proposals")
