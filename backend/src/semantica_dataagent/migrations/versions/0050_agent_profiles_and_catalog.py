"""Add provider catalog snapshot, agent_profiles, user_agent_defaults, agent_runners, and conversation.agent_profile_id (#328)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

BUILTIN_RUNNERS = (
    {
        "id": "pi",
        "display_name": "pi",
        "npm_package": "@earendil-works/pi-coding-agent",
        "package_version": "1.2.0",
        "executable": "pi",
        "adapter_id": "pi",
        "builtin": True,
    },
    {
        "id": "claude",
        "display_name": "Claude",
        "npm_package": "@anthropic-ai/claude-code",
        "package_version": "2.1.318",
        "executable": "claude",
        "adapter_id": "claude",
        "builtin": True,
    },
    {
        "id": "codex",
        "display_name": "Codex",
        "npm_package": "@openai/codex",
        "package_version": "0.166.0",
        "executable": "codex",
        "adapter_id": "codex",
        "builtin": True,
    },
)


def _json_dict(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_str_list(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            trimmed = item.strip()
            if trimmed and trimmed not in result:
                result.append(trimmed)
    return result


def _seed_runners(conn, now: datetime) -> None:
    for runner in BUILTIN_RUNNERS:
        exists = conn.execute(
            sa.text("SELECT 1 FROM agent_runners WHERE id = :id"),
            {"id": runner["id"]},
        ).fetchone()
        if exists is not None:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO agent_runners "
                "(id, display_name, npm_package, package_version, executable, "
                "adapter_id, status_override, builtin, created_at, updated_at) "
                "VALUES (:id, :display_name, :npm_package, :package_version, "
                ":executable, :adapter_id, NULL, :builtin, :created_at, :updated_at)"
            ),
            {
                **runner,
                "created_at": now,
                "updated_at": now,
            },
        )


def _migrate_profiles(conn, now: datetime) -> None:
    settings_rows = conn.execute(
        sa.text(
            "SELECT key, value FROM settings WHERE key IN "
            "('assistant_agent', 'assistant_provider_id', 'assistant_model', "
            "'assistant_agent_profiles')"
        )
    ).fetchall()
    settings = {row[0]: row[1] for row in settings_rows}

    legacy_profiles = _json_dict(settings.get("assistant_agent_profiles"))
    system_agent = (settings.get("assistant_agent") or "").strip() or "pi"
    if system_agent not in {r["id"] for r in BUILTIN_RUNNERS}:
        system_agent = "pi"
    system_provider_id = (settings.get("assistant_provider_id") or "").strip() or None
    system_model = (settings.get("assistant_model") or "").strip() or None

    provider_rows = conn.execute(
        sa.text("SELECT id, models FROM providers ORDER BY name, id")
    ).fetchall()
    providers_by_id = {
        row[0]: _json_str_list(row[1]) for row in provider_rows if row[0]
    }

    already_has_default = (
        conn.execute(
            sa.text("SELECT 1 FROM agent_profiles WHERE is_system_default = 1")
        ).fetchone()
        is not None
    )

    for runner in BUILTIN_RUNNERS:
        runner_id = runner["id"]
        exists = conn.execute(
            sa.text("SELECT 1 FROM agent_profiles WHERE id = :id"),
            {"id": runner_id},
        ).fetchone()
        if exists is not None:
            continue

        legacy = legacy_profiles.get(runner_id)
        legacy_dict = legacy if isinstance(legacy, dict) else {}
        raw_providers = legacy_dict.get("providers")
        if isinstance(raw_providers, list) and any(
            isinstance(p, str) and p for p in raw_providers
        ):
            allowed_pids = [
                p for p in raw_providers if isinstance(p, str) and p in providers_by_id
            ]
        else:
            allowed_pids = list(providers_by_id.keys())

        selected_map: dict[str, list[str]] = {}
        for pid in allowed_pids:
            models = providers_by_id.get(pid, [])
            if models:
                selected_map[pid] = list(models)

        legacy_dp = (
            legacy_dict.get("default_provider")
            if isinstance(legacy_dict.get("default_provider"), str)
            else ""
        ).strip() or None
        legacy_dm = (
            legacy_dict.get("default_model")
            if isinstance(legacy_dict.get("default_model"), str)
            else ""
        ).strip() or None

        if legacy_dp or legacy_dm:
            default_provider_id = legacy_dp
            default_model = legacy_dm
        elif runner_id == system_agent:
            default_provider_id = system_provider_id
            default_model = system_model
        else:
            default_provider_id = None
            default_model = None

        is_default = (not already_has_default) and (runner_id == system_agent)
        if is_default:
            already_has_default = True

        serialized_models = json.dumps(selected_map, ensure_ascii=False)
        conn.execute(
            sa.text(
                "INSERT INTO agent_profiles "
                "(id, name, runner_id, enabled, is_system_default, "
                "default_provider_id, default_model, provider_models, "
                "pending_verification_models, version, created_at, updated_at) "
                "VALUES (:id, :name, :runner_id, 1, :is_system_default, "
                ":default_provider_id, :default_model, :provider_models, "
                ":pending_verification_models, 1, :created_at, :updated_at)"
            ),
            {
                "id": runner_id,
                "name": runner["display_name"],
                "runner_id": runner_id,
                "is_system_default": 1 if is_default else 0,
                "default_provider_id": default_provider_id,
                "default_model": default_model,
                "provider_models": serialized_models,
                "pending_verification_models": serialized_models,
                "created_at": now,
                "updated_at": now,
            },
        )


def _backfill_conversations(conn) -> None:
    builtin_ids = tuple(r["id"] for r in BUILTIN_RUNNERS)
    placeholders = ", ".join(f":id_{i}" for i in range(len(builtin_ids)))
    params = {f"id_{i}": val for i, val in enumerate(builtin_ids)}
    conn.execute(
        sa.text(
            f"UPDATE conversations SET agent_profile_id = agent "
            f"WHERE agent_profile_id IS NULL AND agent IN ({placeholders})"
        ),
        params,
    )


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    now = datetime.now(UTC)

    provider_cols = {col["name"] for col in inspector.get_columns("providers")}
    with op.batch_alter_table("providers") as batch_op:
        if "catalog_models" not in provider_cols:
            batch_op.add_column(sa.Column("catalog_models", sa.Text(), nullable=True))
        if "catalog_fetched_at" not in provider_cols:
            batch_op.add_column(
                sa.Column("catalog_fetched_at", sa.DateTime(), nullable=True)
            )
        if "catalog_status" not in provider_cols:
            batch_op.add_column(sa.Column("catalog_status", sa.Text(), nullable=True))
        if "catalog_error" not in provider_cols:
            batch_op.add_column(sa.Column("catalog_error", sa.Text(), nullable=True))

    if not inspector.has_table("agent_runners"):
        op.create_table(
            "agent_runners",
            sa.Column("id", sa.String(), primary_key=True, nullable=False),
            sa.Column("display_name", sa.String(), nullable=False),
            sa.Column("npm_package", sa.String(), nullable=False),
            sa.Column("package_version", sa.String(), nullable=False),
            sa.Column("executable", sa.String(), nullable=False),
            sa.Column("adapter_id", sa.String(), nullable=False),
            sa.Column("status_override", sa.String(), nullable=True),
            sa.Column(
                "builtin",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if not inspector.has_table("agent_profiles"):
        op.create_table(
            "agent_profiles",
            sa.Column("id", sa.String(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(), nullable=False, unique=True),
            sa.Column("runner_id", sa.String(), nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column(
                "is_system_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("default_provider_id", sa.String(), nullable=True),
            sa.Column("default_model", sa.String(), nullable=True),
            sa.Column(
                "provider_models",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "pending_verification_models",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "uq_agent_profiles_system_default",
            "agent_profiles",
            ["is_system_default"],
            unique=True,
            sqlite_where=sa.text("is_system_default = 1"),
        )

    if not inspector.has_table("user_agent_defaults"):
        op.create_table(
            "user_agent_defaults",
            sa.Column("owner_id", sa.String(), primary_key=True, nullable=False),
            sa.Column(
                "agent_profile_id", sa.String(), primary_key=True, nullable=False
            ),
            sa.Column("provider_id", sa.String(), nullable=False),
            sa.Column("model", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    conv_cols = {col["name"] for col in inspector.get_columns("conversations")}
    if "agent_profile_id" not in conv_cols:
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.add_column(
                sa.Column("agent_profile_id", sa.String(), nullable=True)
            )

    _seed_runners(conn, now)
    _migrate_profiles(conn, now)
    _backfill_conversations(conn)


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    conv_cols = {col["name"] for col in inspector.get_columns("conversations")}
    if "agent_profile_id" in conv_cols:
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.drop_column("agent_profile_id")

    if inspector.has_table("user_agent_defaults"):
        op.drop_table("user_agent_defaults")

    if inspector.has_table("agent_profiles"):
        indexes = {
            idx["name"]
            for idx in inspector.get_indexes("agent_profiles")
            if idx.get("name")
        }
        if "uq_agent_profiles_system_default" in indexes:
            op.drop_index(
                "uq_agent_profiles_system_default", table_name="agent_profiles"
            )
        op.drop_table("agent_profiles")

    if inspector.has_table("agent_runners"):
        op.drop_table("agent_runners")

    provider_cols = {col["name"] for col in inspector.get_columns("providers")}
    drop_cols = [
        col
        for col in (
            "catalog_models",
            "catalog_fetched_at",
            "catalog_status",
            "catalog_error",
        )
        if col in provider_cols
    ]
    if drop_cols:
        with op.batch_alter_table("providers") as batch_op:
            for col in drop_cols:
                batch_op.drop_column(col)
