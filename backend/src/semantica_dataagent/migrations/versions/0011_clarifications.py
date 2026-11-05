"""clarification requests recorded for a caller or conversation"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "clarifications",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String()),
        sa.Column("questions", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "clarifications_conversation", "clarifications", ["conversation_id"]
    )
    op.create_index("clarifications_owner", "clarifications", ["owner_id"])


def downgrade():
    op.drop_index("clarifications_owner", table_name="clarifications")
    op.drop_index("clarifications_conversation", table_name="clarifications")
    op.drop_table("clarifications")
