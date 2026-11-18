"""Extend correction_proposals and builds for structured overrides and revert."""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "correction_proposals",
        sa.Column(
            "source",
            sa.String(),
            nullable=False,
            server_default="manual",
        ),
    )
    op.add_column(
        "correction_proposals",
        sa.Column(
            "target_kind",
            sa.String(),
            nullable=False,
            server_default="document",
        ),
    )
    op.add_column(
        "correction_proposals",
        sa.Column("structured_patch", sa.Text(), nullable=True),
    )
    op.add_column(
        "correction_proposals",
        sa.Column("gate_report", sa.Text(), nullable=True),
    )
    op.add_column(
        "correction_proposals",
        sa.Column("reverted_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "correction_proposals",
        sa.Column("revert_commit_sha", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE correction_proposals "
        "SET target_kind = CASE "
        "WHEN target LIKE 'document:%' THEN 'document' "
        "WHEN target LIKE 'ossie:%' THEN 'ossie' "
        "ELSE 'override' "
        "END"
    )
    op.add_column(
        "builds",
        sa.Column(
            "stale_overrides",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade():
    op.drop_column("builds", "stale_overrides")
    for name in (
        "revert_commit_sha",
        "reverted_at",
        "gate_report",
        "structured_patch",
        "target_kind",
        "source",
    ):
        op.drop_column("correction_proposals", name)
