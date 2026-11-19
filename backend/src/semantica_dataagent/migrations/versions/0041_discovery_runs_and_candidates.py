"""Add discovery_runs and discovery_candidates tables (#304)."""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default="default",
        ),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="running",
        ),
        sa.Column(
            "mine_query_history",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "probe_relationships",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "seed_eval_drafts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "config_snapshot",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "queries_fetched",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "templates_clustered",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "candidates_recalled",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "candidates_probed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "candidates_passed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "candidates_skipped",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "candidates_failed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "proposals_created",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "eval_drafts_created",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_discovery_runs_project_created",
        "discovery_runs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "one_running_discovery_run_per_project",
        "discovery_runs",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column(
            "project_id",
            sa.String(),
            nullable=False,
            server_default="default",
        ),
        sa.Column("candidate_kind", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "evidence_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("proposal_id", sa.String(), nullable=True),
        sa.Column("eval_question_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_discovery_candidates_run_created",
        "discovery_candidates",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_discovery_candidates_project",
        "discovery_candidates",
        ["project_id"],
    )


def downgrade():
    op.drop_index("ix_discovery_candidates_project", table_name="discovery_candidates")
    op.drop_index(
        "ix_discovery_candidates_run_created", table_name="discovery_candidates"
    )
    op.drop_table("discovery_candidates")
    op.drop_index("one_running_discovery_run_per_project", table_name="discovery_runs")
    op.drop_index("ix_discovery_runs_project_created", table_name="discovery_runs")
    op.drop_table("discovery_runs")
