"""Persist metadata pull records."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pull_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("tables", sa.Integer(), nullable=False),
        sa.Column("tasks", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("commit", sa.String()),
        sa.Column("build_id", sa.Integer()),
    )


def downgrade():
    op.drop_table("pull_records")
