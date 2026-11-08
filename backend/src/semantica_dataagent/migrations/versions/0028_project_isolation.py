"""Scope generations, pointers, builds, and settings to a project."""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "default"


def upgrade():
    op.create_table(
        "generation_pointer_new",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
        sa.Column("generation", sa.String(), nullable=False),
    )
    op.execute(
        "INSERT INTO generation_pointer_new (id, project_id, generation) "
        "SELECT id, 'default', generation FROM generation_pointer"
    )
    op.drop_table("generation_pointer")
    op.rename_table("generation_pointer_new", "generation_pointer")
    op.create_index(
        "ix_generation_pointer_project",
        "generation_pointer",
        ["project_id"],
        unique=True,
    )
    op.add_column(
        "knowledge_generations",
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
    )
    op.create_index(
        "ix_knowledge_generations_project",
        "knowledge_generations",
        ["project_id"],
    )
    op.add_column(
        "builds",
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
    )
    op.create_index("ix_builds_project", "builds", ["project_id"])
    op.create_table(
        "project_settings",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )


def downgrade():
    op.drop_table("project_settings")
    op.drop_index("ix_builds_project", table_name="builds")
    op.drop_column("builds", "project_id")
    op.drop_index(
        "ix_knowledge_generations_project", table_name="knowledge_generations"
    )
    op.drop_column("knowledge_generations", "project_id")
    op.create_table(
        "generation_pointer_new",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.String(), nullable=False),
        sa.CheckConstraint("id = 1"),
    )
    op.execute(
        "INSERT INTO generation_pointer_new (id, generation) "
        "SELECT 1, generation FROM generation_pointer WHERE id = 1 LIMIT 1"
    )
    op.drop_index("ix_generation_pointer_project", table_name="generation_pointer")
    op.drop_table("generation_pointer")
    op.rename_table("generation_pointer_new", "generation_pointer")
