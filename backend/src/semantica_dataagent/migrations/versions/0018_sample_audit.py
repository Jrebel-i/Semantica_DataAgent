"""Distinguish sample audit records from query executions."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "query_audit",
        sa.Column("kind", sa.String(), nullable=False, server_default="query"),
    )


def downgrade():
    with op.batch_alter_table("query_audit") as batch:
        batch.drop_column("kind")
