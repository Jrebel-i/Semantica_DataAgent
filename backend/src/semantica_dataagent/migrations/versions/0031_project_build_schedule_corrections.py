"""Scope corrections, knowledge gaps and metadata overrides to a project."""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "default"


def upgrade():
    op.drop_index("one_running_build", table_name="builds")
    op.create_index(
        "one_running_build_per_project",
        "builds",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )
    op.add_column(
        "correction_proposals",
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
    )
    op.create_index(
        "ix_correction_proposals_project",
        "correction_proposals",
        ["project_id"],
    )
    op.add_column(
        "knowledge_gaps",
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
    )
    op.create_index(
        "ix_knowledge_gaps_project",
        "knowledge_gaps",
        ["project_id"],
    )
    op.create_table(
        "metadata_overrides_new",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("target", sa.String(), primary_key=True),
        sa.Column("comment", sa.String(), nullable=False),
    )
    op.execute(
        "INSERT INTO metadata_overrides_new (project_id, target, comment) "
        "SELECT 'default', target, comment FROM metadata_overrides"
    )
    op.drop_table("metadata_overrides")
    op.rename_table("metadata_overrides_new", "metadata_overrides")


def downgrade():
    op.drop_index("one_running_build_per_project", table_name="builds")
    op.create_index(
        "one_running_build",
        "builds",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "metadata_overrides_old",
        sa.Column("target", sa.String(), primary_key=True),
        sa.Column("comment", sa.String(), nullable=False),
    )
    op.execute(
        "INSERT INTO metadata_overrides_old (target, comment) "
        "SELECT target, comment FROM metadata_overrides WHERE project_id = 'default'"
    )
    op.drop_table("metadata_overrides")
    op.rename_table("metadata_overrides_old", "metadata_overrides")
    op.drop_index("ix_knowledge_gaps_project", table_name="knowledge_gaps")
    op.drop_column("knowledge_gaps", "project_id")
    op.drop_index("ix_correction_proposals_project", table_name="correction_proposals")
    op.drop_column("correction_proposals", "project_id")
