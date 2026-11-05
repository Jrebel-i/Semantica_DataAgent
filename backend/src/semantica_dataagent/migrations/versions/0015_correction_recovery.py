"""Persist recovery information for document approvals."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "builds", sa.Column("correction_proposal_id", sa.String(), nullable=True)
    )
    op.create_index(
        "builds_correction_proposal", "builds", ["correction_proposal_id"], unique=True
    )
    for name in (
        "original_patch",
        "source_directory",
        "source_path",
        "reviewer_name",
        "commit_sha",
    ):
        op.add_column("correction_proposals", sa.Column(name, sa.Text(), nullable=True))
    op.execute("UPDATE correction_proposals SET original_patch = patch")


def downgrade():
    op.drop_index("builds_correction_proposal", table_name="builds")
    op.drop_column("builds", "correction_proposal_id")
    for name in (
        "commit_sha",
        "reviewer_name",
        "source_path",
        "source_directory",
        "original_patch",
    ):
        op.drop_column("correction_proposals", name)
