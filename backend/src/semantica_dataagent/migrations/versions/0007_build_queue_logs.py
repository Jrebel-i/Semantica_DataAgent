import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("builds", sa.Column("logs", sa.Text, nullable=True))


def downgrade():
    op.drop_column("builds", "logs")
