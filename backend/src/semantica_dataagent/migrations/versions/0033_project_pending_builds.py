"""Persist debounced auto-build requests per project."""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_pending_builds",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("due_at", sa.String(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("requested_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_project_pending_builds_due_at",
        "project_pending_builds",
        ["due_at"],
    )


def downgrade():
    op.drop_index(
        "ix_project_pending_builds_due_at", table_name="project_pending_builds"
    )
    op.drop_table("project_pending_builds")
