import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    for name, column_type in [
        ("build_type", sa.String),
        ("triggered_by", sa.String),
        ("source_commit", sa.String),
        ("generation", sa.String),
        ("source_hashes", sa.Text),
        ("config_hash", sa.String),
        ("affected_files", sa.Text),
        ("dimension_tables", sa.Text),
        ("generated_files", sa.Text),
    ]:
        op.add_column("builds", sa.Column(name, column_type, nullable=True))


def downgrade():
    for name in (
        "generated_files",
        "dimension_tables",
        "affected_files",
        "config_hash",
        "source_hashes",
        "generation",
        "source_commit",
        "triggered_by",
        "build_type",
    ):
        op.drop_column("builds", name)
