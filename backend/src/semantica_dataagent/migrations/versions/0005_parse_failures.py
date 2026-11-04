import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "builds",
        sa.Column("parse_failures", sa.Text, nullable=False, server_default="[]"),
    )


def downgrade():
    op.drop_column("builds", "parse_failures")
