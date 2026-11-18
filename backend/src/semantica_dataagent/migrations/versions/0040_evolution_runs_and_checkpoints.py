"""Add evolution_runs, evolution_rounds, and evolution_checkpoints tables."""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evolution_checkpoints",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("checkpoint_trajectory_id", sa.Integer(), nullable=True),
        sa.Column("checkpoint_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_id", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "evolution_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default="default",
        ),
        sa.Column(
            "trigger_type",
            sa.String(),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="running",
        ),
        sa.Column("checkpoint_before_trajectory_id", sa.Integer(), nullable=True),
        sa.Column("checkpoint_after_trajectory_id", sa.Integer(), nullable=True),
        sa.Column("frozen_max_trajectory_id", sa.Integer(), nullable=True),
        sa.Column(
            "diagnosis_trajectory_ids",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "reserve_trajectory_ids",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "signal_summary",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_eval_cases", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("rounds_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("proposal_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_evolution_runs_project_created",
        "evolution_runs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "one_running_evolution_run_per_project",
        "evolution_runs",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "evolution_rounds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default="default",
        ),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("signal_kind", sa.String(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column(
            "evidence_trajectory_ids",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("target", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "target_kind",
            sa.String(),
            nullable=False,
            server_default="override",
        ),
        sa.Column(
            "candidate_change",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("l1_gate_report", sa.Text(), nullable=True),
        sa.Column("l2_gate_report", sa.Text(), nullable=True),
        sa.Column(
            "decision",
            sa.String(),
            nullable=False,
            server_default="running",
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("proposal_id", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_evolution_rounds_run_round",
        "evolution_rounds",
        ["run_id", "round_number"],
    )
    op.create_index(
        "ix_evolution_rounds_project",
        "evolution_rounds",
        ["project_id"],
    )


def downgrade():
    op.drop_index("ix_evolution_rounds_project", table_name="evolution_rounds")
    op.drop_index("ix_evolution_rounds_run_round", table_name="evolution_rounds")
    op.drop_table("evolution_rounds")
    op.drop_index("one_running_evolution_run_per_project", table_name="evolution_runs")
    op.drop_index("ix_evolution_runs_project_created", table_name="evolution_runs")
    op.drop_table("evolution_runs")
    op.drop_table("evolution_checkpoints")
