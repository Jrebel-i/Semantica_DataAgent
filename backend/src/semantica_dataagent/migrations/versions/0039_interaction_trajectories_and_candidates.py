"""Add interaction_trajectories table and knowledge_generations.is_candidate."""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_generations",
        sa.Column(
            "is_candidate",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "interaction_trajectories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default="default",
        ),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("session_key", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("token_id", sa.String(), nullable=True),
        sa.Column("langfuse_trace_id", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="running",
        ),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("matched_refs", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("clarifications", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("sql_validations", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("final_sql", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("citations", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("window_started_at", sa.DateTime(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_interaction_trajectories_project_recorded_id",
        "interaction_trajectories",
        ["project_id", "recorded_at", "id"],
    )
    op.create_index(
        "ix_interaction_trajectories_project_id_cursor",
        "interaction_trajectories",
        ["project_id", "id"],
    )
    op.create_index(
        "ix_interaction_trajectories_conv_turn",
        "interaction_trajectories",
        ["conversation_id", "turn_id"],
    )
    op.create_index(
        "ix_interaction_trajectories_local_window",
        "interaction_trajectories",
        ["project_id", "token_id", "source_type", "last_activity_at"],
    )
    op.create_index(
        "ix_interaction_trajectories_session_key",
        "interaction_trajectories",
        ["session_key"],
        unique=True,
    )


def downgrade():
    op.drop_index(
        "ix_interaction_trajectories_session_key",
        table_name="interaction_trajectories",
    )
    op.drop_index(
        "ix_interaction_trajectories_local_window",
        table_name="interaction_trajectories",
    )
    op.drop_index(
        "ix_interaction_trajectories_conv_turn",
        table_name="interaction_trajectories",
    )
    op.drop_index(
        "ix_interaction_trajectories_project_id_cursor",
        table_name="interaction_trajectories",
    )
    op.drop_index(
        "ix_interaction_trajectories_project_recorded_id",
        table_name="interaction_trajectories",
    )
    op.drop_table("interaction_trajectories")
    op.drop_column("knowledge_generations", "is_candidate")
