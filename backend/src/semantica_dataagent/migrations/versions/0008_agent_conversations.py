"""agent providers, conversations, turns and event summaries"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "providers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_key", sa.Text()),
        sa.Column("models", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("conversations_owner", "conversations", ["owner_id"])
    op.create_table(
        "turns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("citations", sa.Text(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_turn_sequence"),
    )
    op.create_table(
        "turn_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.UniqueConstraint("turn_id", "seq", name="uq_turn_event_seq"),
    )
    op.create_index("turn_events_turn", "turn_events", ["turn_id"])


def downgrade():
    op.drop_index("turn_events_turn", table_name="turn_events")
    op.drop_table("turn_events")
    op.drop_table("turns")
    op.drop_index("conversations_owner", table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("providers")
