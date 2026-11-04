import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plugin_config",
        sa.Column("category", sa.String, primary_key=True),
        sa.Column("plugin_id", sa.String, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("parameters", sa.Text, nullable=False),
    )


def downgrade():
    op.drop_table("plugin_config")
