"""Add human review state to knowledge gaps."""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_gaps",
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
    )
    op.add_column(
        "knowledge_gaps", sa.Column("reviewed_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "knowledge_gaps", sa.Column("reviewer_id", sa.String(), nullable=True)
    )
    op.add_column(
        "knowledge_gaps", sa.Column("reviewer_name", sa.String(), nullable=True)
    )
    op.add_column(
        "correction_proposals",
        sa.Column("knowledge_gap_id", sa.String(), nullable=True),
    )


def downgrade():
    op.drop_column("correction_proposals", "knowledge_gap_id")
    for name in ("reviewer_name", "reviewer_id", "reviewed_at", "status"):
        op.drop_column("knowledge_gaps", name)
