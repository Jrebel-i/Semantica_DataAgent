"""Archive status and durable project deletion progress (#190)."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_deletions",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("project_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="deleting"),
        sa.Column("step", sa.String(), nullable=False, server_default="begin"),
        sa.Column("steps", sa.String(), nullable=False, server_default="[]"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("project_deletions")
