"""Scope source adapter and puller configuration to a project."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

DEFAULT_PROJECT_ID = "default"


def upgrade():
    op.create_table(
        "plugin_config_new",
        sa.Column(
            "project_id",
            sa.String(),
            primary_key=True,
            nullable=False,
            server_default=DEFAULT_PROJECT_ID,
        ),
        sa.Column("category", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("parameters", sa.String(), nullable=False),
    )
    op.execute(
        "INSERT INTO plugin_config_new (project_id, category, plugin_id, enabled, parameters) "
        "SELECT 'default', category, plugin_id, enabled, parameters FROM plugin_config"
    )
    op.drop_table("plugin_config")
    op.rename_table("plugin_config_new", "plugin_config")


def downgrade():
    op.create_table(
        "plugin_config_old",
        sa.Column("category", sa.String(), primary_key=True),
        sa.Column("plugin_id", sa.String(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("parameters", sa.String(), nullable=False),
    )
    op.execute(
        "INSERT INTO plugin_config_old (category, plugin_id, enabled, parameters) "
        "SELECT category, plugin_id, enabled, parameters FROM plugin_config "
        "WHERE project_id = 'default'"
    )
    op.drop_table("plugin_config")
    op.rename_table("plugin_config_old", "plugin_config")
