"""Add project_pull_instances table and extend pull_records and builds (#316)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def _read_legacy_pull_secret(conn, project_id: str, old_pid: str) -> str | None:
    key = f"puller.{old_pid}.token"
    row = conn.execute(
        sa.text(
            "SELECT value FROM project_settings WHERE project_id = :pid AND key = :key"
        ),
        {"pid": project_id, "key": key},
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    if project_id == "default":
        legacy = conn.execute(
            sa.text("SELECT value FROM settings WHERE key = :key"),
            {"key": key},
        ).fetchone()
        if legacy and legacy[0]:
            return str(legacy[0])
    return None


def _migrate_legacy_pullers(conn) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT project_id, plugin_id, enabled, parameters "
            "FROM plugin_config "
            "WHERE category = 'puller' AND plugin_id IN ('ossie', 'dtb') "
            "ORDER BY project_id ASC, CASE WHEN plugin_id = 'ossie' THEN 0 ELSE 1 END"
        )
    ).fetchall()
    by_project: dict[str, list[tuple[str, bool, str | None]]] = {}
    for project_id, plugin_id, enabled, raw_params in rows:
        by_project.setdefault(str(project_id), []).append(
            (str(plugin_id), bool(enabled), raw_params)
        )

    now = datetime.now(UTC)
    for project_id, items in by_project.items():
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM project_pull_instances "
                "WHERE project_id = :pid AND id = 'dtb'"
            ),
            {"pid": project_id},
        ).fetchone()
        if exists is not None:
            continue

        chosen_pid, chosen_enabled, chosen_raw = next(
            (item for item in items if item[1]), items[0]
        )
        try:
            parsed = json.loads(chosen_raw or "{}")
        except (TypeError, ValueError):
            parsed = {}
        params = dict(parsed) if isinstance(parsed, dict) else {}
        params.pop("token", None)
        raw_interval = params.pop("interval_seconds", None)
        ciphertext = _read_legacy_pull_secret(conn, project_id, chosen_pid)

        if not chosen_enabled and not params and not ciphertext:
            continue

        has_interval = (
            isinstance(raw_interval, int)
            and not isinstance(raw_interval, bool)
            and raw_interval > 0
        )
        schedule_cron = "0 2 * * *" if has_interval else None
        encrypted_secrets = json.dumps({"token": ciphertext}) if ciphertext else "{}"

        conn.execute(
            sa.text(
                "INSERT INTO project_pull_instances "
                "(project_id, id, plugin_id, display_name, enabled, parameters, "
                "encrypted_secrets, schedule_cron, timezone, catalog_prefix, "
                "task_namespace, drop_threshold, last_table_count, "
                "last_triggered_at, created_at, updated_at) "
                "VALUES (:project_id, 'dtb', 'dtb', 'DTB', :enabled, :parameters, "
                ":encrypted_secrets, :schedule_cron, 'Asia/Shanghai', NULL, NULL, "
                "0.5, NULL, NULL, :created_at, :updated_at)"
            ),
            {
                "project_id": project_id,
                "enabled": chosen_enabled,
                "parameters": json.dumps(params, ensure_ascii=False),
                "encrypted_secrets": encrypted_secrets,
                "schedule_cron": schedule_cron,
                "created_at": now,
                "updated_at": now,
            },
        )


def upgrade():
    op.create_table(
        "project_pull_instances",
        sa.Column("project_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("parameters", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("encrypted_secrets", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("schedule_cron", sa.String(), nullable=True),
        sa.Column(
            "timezone",
            sa.String(),
            nullable=False,
            server_default="Asia/Shanghai",
        ),
        sa.Column("catalog_prefix", sa.String(), nullable=True),
        sa.Column("task_namespace", sa.String(), nullable=True),
        sa.Column("drop_threshold", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("last_table_count", sa.Integer(), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_project_pull_instances_project",
        "project_pull_instances",
        ["project_id"],
    )

    op.add_column(
        "pull_records",
        sa.Column("instance_id", sa.String(), nullable=True),
    )
    op.add_column(
        "pull_records",
        sa.Column("plugin_id", sa.String(), nullable=True),
    )
    op.add_column(
        "pull_records",
        sa.Column(
            "trigger_type",
            sa.String(),
            nullable=False,
            server_default="manual",
        ),
    )
    op.add_column(
        "pull_records",
        sa.Column("skipped_reason", sa.String(), nullable=True),
    )
    op.add_column(
        "pull_records",
        sa.Column("warnings", sa.Text(), nullable=False, server_default="[]"),
    )

    op.add_column(
        "builds",
        sa.Column("entity_hashes", sa.Text(), nullable=True),
    )
    op.add_column(
        "builds",
        sa.Column("fallback_reason", sa.String(), nullable=True),
    )

    _migrate_legacy_pullers(op.get_bind())
    _migrate_legacy_source_adapters(op.get_bind())


def _migrate_legacy_source_adapters(conn) -> None:
    """Enable ossie + canonical for projects that previously relied on retired source/dtb."""
    dtb_rows = conn.execute(
        sa.text(
            "SELECT project_id, enabled FROM plugin_config "
            "WHERE category = 'source' AND plugin_id = 'dtb'"
        )
    ).fetchall()
    for project_id, dtb_enabled in dtb_rows:
        pid = str(project_id)
        if bool(dtb_enabled):
            ossie_row = conn.execute(
                sa.text(
                    "SELECT enabled FROM plugin_config "
                    "WHERE project_id = :pid AND category = 'source' AND plugin_id = 'ossie'"
                ),
                {"pid": pid},
            ).fetchone()
            if ossie_row is None:
                conn.execute(
                    sa.text(
                        "INSERT INTO plugin_config "
                        "(project_id, category, plugin_id, enabled, parameters) "
                        "VALUES (:pid, 'source', 'ossie', 1, '{\"path\": \"ossie\"}')"
                    ),
                    {"pid": pid},
                )
            canonical_row = conn.execute(
                sa.text(
                    "SELECT enabled FROM plugin_config "
                    "WHERE project_id = :pid AND category = 'source' AND plugin_id = 'canonical'"
                ),
                {"pid": pid},
            ).fetchone()
            if canonical_row is not None and not bool(canonical_row[0]):
                conn.execute(
                    sa.text(
                        "UPDATE plugin_config SET enabled = 1 "
                        "WHERE project_id = :pid AND category = 'source' AND plugin_id = 'canonical'"
                    ),
                    {"pid": pid},
                )
        conn.execute(
            sa.text(
                "DELETE FROM plugin_config "
                "WHERE project_id = :pid AND category = 'source' AND plugin_id = 'dtb'"
            ),
            {"pid": pid},
        )


def downgrade():
    with op.batch_alter_table("builds") as batch_op:
        batch_op.drop_column("fallback_reason")
        batch_op.drop_column("entity_hashes")
    with op.batch_alter_table("pull_records") as batch_op:
        batch_op.drop_column("warnings")
        batch_op.drop_column("skipped_reason")
        batch_op.drop_column("trigger_type")
        batch_op.drop_column("plugin_id")
        batch_op.drop_column("instance_id")
    op.drop_index(
        "ix_project_pull_instances_project", table_name="project_pull_instances"
    )
    op.drop_table("project_pull_instances")
