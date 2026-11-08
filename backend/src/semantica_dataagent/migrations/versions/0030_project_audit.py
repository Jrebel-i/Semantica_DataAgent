"""Add project audit records for project and membership changes."""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("actor_name", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_project_audit_project_created",
        "project_audit",
        ["project_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_project_audit_project_created", table_name="project_audit")
    op.drop_table("project_audit")
