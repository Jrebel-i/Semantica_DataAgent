"""Add projects and project members, and seed the default business project."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_NAME = "业务数据"


def upgrade():
    op.create_table(
        "projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("token_id", sa.String(), primary_key=True),
        sa.Column("role", sa.String(), nullable=False),
    )

    connection = op.get_bind()
    created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    connection.execute(
        sa.text(
            "INSERT INTO projects (id, name, type, status, created_at) "
            "SELECT :id, :name, 'warehouse', 'active', :created_at "
            "WHERE NOT EXISTS (SELECT 1 FROM projects WHERE id = :id)"
        ),
        {
            "id": DEFAULT_PROJECT_ID,
            "name": DEFAULT_PROJECT_NAME,
            "created_at": created_at,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO project_members (project_id, token_id, role) "
            "SELECT :project_id, tokens.id, 'member' FROM tokens "
            "WHERE tokens.revoked = 0 AND tokens.role = 'user' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM project_members "
            "WHERE project_members.project_id = :project_id "
            "AND project_members.token_id = tokens.id)"
        ),
        {"project_id": DEFAULT_PROJECT_ID},
    )


def downgrade():
    op.drop_table("project_members")
    op.drop_table("projects")
