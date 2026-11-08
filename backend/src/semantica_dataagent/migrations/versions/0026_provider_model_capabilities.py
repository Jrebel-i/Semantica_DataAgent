"""Add per-model capabilities to providers."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "providers", sa.Column("model_capabilities", sa.Text(), nullable=True)
    )


def downgrade():
    op.drop_column("providers", "model_capabilities")
