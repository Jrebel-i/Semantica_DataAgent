"""Record the provider and model used by each turn."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("turns", sa.Column("provider_id", sa.String(), nullable=True))
    op.add_column("turns", sa.Column("model", sa.String(), nullable=True))


def downgrade():
    op.drop_column("turns", "model")
    op.drop_column("turns", "provider_id")
