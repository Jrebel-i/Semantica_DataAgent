"""Associate query results with their online conversation."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "query_executions", sa.Column("conversation_id", sa.String(), nullable=True)
    )


def downgrade():
    op.drop_column("query_executions", "conversation_id")
