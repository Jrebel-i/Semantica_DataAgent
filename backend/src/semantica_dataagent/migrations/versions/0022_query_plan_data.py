"""Preserve the complete query plan and the turn which requested it."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("query_executions", sa.Column("turn_id", sa.Integer(), nullable=True))
    op.add_column("query_executions", sa.Column("plan_data", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("query_executions", "plan_data")
    op.drop_column("query_executions", "turn_id")
