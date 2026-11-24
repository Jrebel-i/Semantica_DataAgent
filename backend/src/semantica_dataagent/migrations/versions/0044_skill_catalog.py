"""Persist uploaded Skill packages and their audit trail atomically."""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "catalog_skills",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.String(), nullable=False),
        sa.Column("archive", sa.LargeBinary(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "skill_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("skill_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "catalog_skill_projects",
        sa.Column("skill_name", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), primary_key=True),
    )


def downgrade():
    op.drop_table("catalog_skill_projects")
    op.drop_table("skill_audit")
    op.drop_table("catalog_skills")
