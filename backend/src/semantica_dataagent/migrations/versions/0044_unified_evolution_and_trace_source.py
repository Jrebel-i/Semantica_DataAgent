"""Add project_evolution_configs and retire interaction_trajectories & is_candidate (#319)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_TRAJECTORY_INDEXES = (
    "ix_interaction_trajectories_session_key",
    "ix_interaction_trajectories_local_window",
    "ix_interaction_trajectories_conv_turn",
    "ix_interaction_trajectories_project_id_cursor",
    "ix_interaction_trajectories_project_recorded_id",
)


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table("project_evolution_configs"):
        op.create_table(
            "project_evolution_configs",
            sa.Column("project_id", sa.String(), primary_key=True, nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("trace_source_plugin", sa.String(), nullable=True),
            sa.Column(
                "trace_source_parameters",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "trace_source_encrypted_secrets",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column("schedule_cron", sa.String(), nullable=True),
            sa.Column(
                "timezone",
                sa.String(),
                nullable=False,
                server_default="Asia/Shanghai",
            ),
            sa.Column("last_trace_timestamp", sa.DateTime(), nullable=True),
            sa.Column("provider_id", sa.String(), nullable=True),
            sa.Column("model", sa.String(), nullable=True),
            sa.Column("last_triggered_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if inspector.has_table("interaction_trajectories"):
        existing_indexes = {
            idx["name"]
            for idx in inspector.get_indexes("interaction_trajectories")
            if idx.get("name")
        }
        for idx_name in _TRAJECTORY_INDEXES:
            if idx_name in existing_indexes:
                op.drop_index(idx_name, table_name="interaction_trajectories")
        op.drop_table("interaction_trajectories")

    if inspector.has_table("knowledge_generations"):
        kg_columns = {
            col["name"] for col in inspector.get_columns("knowledge_generations")
        }
        if "is_candidate" in kg_columns:
            conn.execute(
                sa.text("DELETE FROM knowledge_generations WHERE is_candidate = 1")
            )
            with op.batch_alter_table("knowledge_generations") as batch_op:
                batch_op.drop_column("is_candidate")

    if inspector.has_table("project_settings"):
        conn.execute(
            sa.text(
                "DELETE FROM project_settings WHERE key = 'evolution_require_review'"
            )
        )


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if inspector.has_table("knowledge_generations"):
        kg_columns = {
            col["name"] for col in inspector.get_columns("knowledge_generations")
        }
        if "is_candidate" not in kg_columns:
            op.add_column(
                "knowledge_generations",
                sa.Column(
                    "is_candidate",
                    sa.Boolean(),
                    nullable=False,
                    server_default="0",
                ),
            )

    if not inspector.has_table("interaction_trajectories"):
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
            sa.Column(
                "sql_validations", sa.Text(), nullable=False, server_default="[]"
            ),
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

    if inspector.has_table("project_evolution_configs"):
        op.drop_table("project_evolution_configs")
