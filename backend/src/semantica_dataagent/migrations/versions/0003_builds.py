import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "builds",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("started_at", sa.String, nullable=False),
        sa.Column("finished_at", sa.String),
        sa.Column("counts", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
    )
    op.create_index(
        "one_running_build",
        "builds",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade():
    op.drop_index("one_running_build", table_name="builds")
    op.drop_table("builds")
