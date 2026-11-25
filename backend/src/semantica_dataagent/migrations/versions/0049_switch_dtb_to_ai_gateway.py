"""Migrate legacy DTB executor/puller plugins and instances to dtb_ai_gateway."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

AI_GOV_DTB_URL = "https://ai-gov.pkfare.io/api/v1/gateway/call/ai-dtb"
LEGACY_DIRECT_URL = "https://dtb.pkfare.io"

_EXECUTOR_ALLOWED_KEYS = frozenset(
    {
        "base_url",
        "env",
        "data_source",
        "row_limit",
        "download_limit",
        "timeout_seconds",
        "confirmation_ttl_seconds",
        "interpret_results",
    }
)
_PULLER_ALLOWED_KEYS = frozenset(
    {
        "url",
        "token",
        "layer",
        "max_tables",
        "model_name",
        "low_cardinality_limit",
        "sample_limit",
        "dimension_row_limit",
        "env",
        "data_source",
        "metadata_row_limit",
    }
)
_GATEWAY_ONLY_PULLER_KEYS = ("env", "data_source", "metadata_row_limit")
_GATEWAY_ONLY_EXECUTOR_KEYS = ("env", "data_source")


def _parse_params(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _rewrite_legacy_url(raw_url: object) -> object:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return raw_url
    parsed = urlsplit(raw_url.strip())
    if (parsed.hostname or "").lower() == "dtb.pkfare.io":
        return AI_GOV_DTB_URL
    return raw_url


def _migrate_plugin_config_category(
    conn, category: str, allowed_keys: frozenset[str], url_key: str
) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT project_id, parameters FROM plugin_config "
            "WHERE category = :category AND plugin_id = 'dtb'"
        ),
        {"category": category},
    ).fetchall()
    for project_id, raw_params in rows:
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM plugin_config "
                "WHERE project_id = :pid AND category = :category "
                "AND plugin_id = 'dtb_ai_gateway'"
            ),
            {"pid": project_id, "category": category},
        ).fetchone()
        if exists is not None:
            conn.execute(
                sa.text(
                    "DELETE FROM plugin_config "
                    "WHERE project_id = :pid AND category = :category AND plugin_id = 'dtb'"
                ),
                {"pid": project_id, "category": category},
            )
            continue

        params = {
            k: v for k, v in _parse_params(raw_params).items() if k in allowed_keys
        }
        if url_key in params:
            params[url_key] = _rewrite_legacy_url(params[url_key])
        conn.execute(
            sa.text(
                "UPDATE plugin_config "
                "SET plugin_id = 'dtb_ai_gateway', parameters = :params "
                "WHERE project_id = :pid AND category = :category AND plugin_id = 'dtb'"
            ),
            {
                "pid": project_id,
                "category": category,
                "params": json.dumps(params, ensure_ascii=False),
            },
        )


def _migrate_pull_instances(conn) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT project_id, id, display_name, parameters "
            "FROM project_pull_instances WHERE plugin_id = 'dtb'"
        )
    ).fetchall()
    now = datetime.now(UTC)
    for project_id, inst_id, display_name, raw_params in rows:
        params = {
            k: v
            for k, v in _parse_params(raw_params).items()
            if k in _PULLER_ALLOWED_KEYS
        }
        if "url" in params:
            params["url"] = _rewrite_legacy_url(params["url"])
        new_display = "DTB (AI Gov 网关)" if display_name == "DTB" else display_name
        conn.execute(
            sa.text(
                "UPDATE project_pull_instances "
                "SET plugin_id = 'dtb_ai_gateway', display_name = :display_name, "
                "parameters = :params, updated_at = :updated_at "
                "WHERE project_id = :pid AND id = :id"
            ),
            {
                "pid": project_id,
                "id": inst_id,
                "display_name": new_display,
                "params": json.dumps(params, ensure_ascii=False),
                "updated_at": now,
            },
        )


def upgrade():
    conn = op.get_bind()
    _migrate_plugin_config_category(
        conn, "executor", _EXECUTOR_ALLOWED_KEYS, "base_url"
    )
    _migrate_plugin_config_category(conn, "puller", _PULLER_ALLOWED_KEYS, "url")
    _migrate_pull_instances(conn)


def _revert_ai_gov_url(raw_url: object) -> object:
    if isinstance(raw_url, str) and raw_url.strip().rstrip("/") == AI_GOV_DTB_URL:
        return LEGACY_DIRECT_URL
    return raw_url


def downgrade():
    conn = op.get_bind()
    for category, strip_keys, url_key in (
        ("executor", _GATEWAY_ONLY_EXECUTOR_KEYS, "base_url"),
        ("puller", _GATEWAY_ONLY_PULLER_KEYS, "url"),
    ):
        rows = conn.execute(
            sa.text(
                "SELECT project_id, parameters FROM plugin_config "
                "WHERE category = :category AND plugin_id = 'dtb_ai_gateway'"
            ),
            {"category": category},
        ).fetchall()
        for project_id, raw_params in rows:
            exists = conn.execute(
                sa.text(
                    "SELECT 1 FROM plugin_config "
                    "WHERE project_id = :pid AND category = :category "
                    "AND plugin_id = 'dtb'"
                ),
                {"pid": project_id, "category": category},
            ).fetchone()
            if exists is not None:
                continue
            params = {
                k: v
                for k, v in _parse_params(raw_params).items()
                if k not in strip_keys
            }
            if url_key in params:
                params[url_key] = _revert_ai_gov_url(params[url_key])
            conn.execute(
                sa.text(
                    "UPDATE plugin_config SET plugin_id = 'dtb', parameters = :params "
                    "WHERE project_id = :pid AND category = :category "
                    "AND plugin_id = 'dtb_ai_gateway'"
                ),
                {
                    "pid": project_id,
                    "category": category,
                    "params": json.dumps(params, ensure_ascii=False),
                },
            )

    inst_rows = conn.execute(
        sa.text(
            "SELECT project_id, id, display_name, parameters "
            "FROM project_pull_instances WHERE plugin_id = 'dtb_ai_gateway'"
        )
    ).fetchall()
    now = datetime.now(UTC)
    for project_id, inst_id, display_name, raw_params in inst_rows:
        params = {
            k: v
            for k, v in _parse_params(raw_params).items()
            if k not in _GATEWAY_ONLY_PULLER_KEYS
        }
        if "url" in params:
            params["url"] = _revert_ai_gov_url(params["url"])
        old_display = "DTB" if display_name == "DTB (AI Gov 网关)" else display_name
        conn.execute(
            sa.text(
                "UPDATE project_pull_instances "
                "SET plugin_id = 'dtb', display_name = :display_name, "
                "parameters = :params, updated_at = :updated_at "
                "WHERE project_id = :pid AND id = :id"
            ),
            {
                "pid": project_id,
                "id": inst_id,
                "display_name": old_display,
                "params": json.dumps(params, ensure_ascii=False),
                "updated_at": now,
            },
        )
