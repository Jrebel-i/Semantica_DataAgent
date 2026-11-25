"""Comprehensive offline integration tests for Issue #304 (FK probing, discovery runs, forced-pending proposals)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _alembic_config(db_path: Path) -> Config:
    from semantica_dataagent import cli

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _init_git_source_dir(source_dir: Path) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "README.md").write_text("# Source\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source_dir, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=source_dir, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Init",
            "-c",
            "user.email=init@example.com",
            "commit",
            "-qm",
            "initial commit",
        ],
        cwd=source_dir,
        check=True,
    )


def _save_personal_token(owner_id: str, plaintext: str) -> None:
    from semantica_dataagent.crypto import encrypt
    from semantica_dataagent.db import PersonalDtbToken, sessions

    with sessions().begin() as db:
        row = db.get(PersonalDtbToken, owner_id)
        if row is None:
            db.add(
                PersonalDtbToken(owner_id=owner_id, encrypted_token=encrypt(plaintext))
            )
        else:
            row.encrypted_token = encrypt(plaintext)


def _enable_dtb_executor(
    base_url: str = "http://dtb.local", timeout_seconds: int = 30
) -> None:
    from semantica_dataagent import project_context
    from semantica_dataagent.db import sessions

    with sessions().begin() as db:
        project_context.upsert_plugin_config(
            db,
            "executor",
            "dtb_ai_gateway",
            True,
            {
                "base_url": base_url,
                "row_limit": 5000,
                "download_limit": 20000,
                "timeout_seconds": timeout_seconds,
            },
        )


def _sample_tables() -> list[dict]:
    return [
        {
            "database": "shop",
            "name": "dwd_orders",
            "ref": "table:shop.dwd_orders",
            "comment": "订单明细表",
            "fields": [
                {
                    "name": "order_id",
                    "type": "bigint",
                    "comment": "订单ID",
                    "partition": False,
                },
                {
                    "name": "user_id",
                    "type": "bigint",
                    "comment": "下单用户ID",
                    "partition": False,
                },
                {
                    "name": "city_code",
                    "type": "varchar(32)",
                    "comment": "城市编码",
                    "partition": False,
                },
                {
                    "name": "amount",
                    "type": "decimal(18,2)",
                    "comment": "订单金额",
                    "partition": False,
                },
                {
                    "name": "status",
                    "type": "varchar(32)",
                    "comment": "订单状态",
                    "partition": False,
                },
                {
                    "name": "dt",
                    "type": "varchar(16)",
                    "comment": "分区日期",
                    "partition": True,
                },
            ],
            "relationships": [],
        },
        {
            "database": "shop",
            "name": "dim_user",
            "ref": "table:shop.dim_user",
            "comment": "用户维表",
            "fields": [
                {
                    "name": "id",
                    "type": "bigint",
                    "comment": "用户主键ID",
                    "partition": False,
                },
                {
                    "name": "user_name",
                    "type": "varchar(64)",
                    "comment": "用户名",
                    "partition": False,
                },
                {
                    "name": "dt",
                    "type": "varchar(16)",
                    "comment": "分区日期",
                    "partition": True,
                },
            ],
            "relationships": [],
        },
        {
            "database": "shop",
            "name": "dim_city",
            "ref": "table:shop.dim_city",
            "comment": "城市维表",
            "fields": [
                {
                    "name": "code",
                    "type": "varchar(32)",
                    "comment": "城市编码",
                    "partition": False,
                },
                {
                    "name": "city_name",
                    "type": "varchar(64)",
                    "comment": "城市名称",
                    "partition": False,
                },
            ],
            "relationships": [],
        },
    ]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "app.db"
    source_dir = tmp_path / "source"
    _init_git_source_dir(source_dir)
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Setting, Token, digest, sessions

    admin_token = initialize()
    with sessions().begin() as db:
        owner_id = db.get(Token, digest(admin_token)).id
        db.add(Setting(key="source_directory", value=str(source_dir)))
    with TestClient(app) as client:
        yield client, admin_token, owner_id, db_path, source_dir


def _wait_run_terminal(
    client: TestClient, token: str, run_id: str, project: str = "default"
) -> dict:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        resp = client.get(
            f"/api/v1/discovery/runs/{run_id}",
            params={"project": project},
            headers=_headers(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        if data["status"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError(f"Discovery run {run_id} did not finish within timeout")


def test_alembic_0041_single_head_upgrade_downgrade_and_partial_unique_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "migrate_0041.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())

    cfg = _alembic_config(db_path)
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0050"]

    command.upgrade(cfg, "0040")
    command.upgrade(cfg, "0041")

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"discovery_runs", "discovery_candidates"}.issubset(tables)

        now = "2026-11-19T00:00:00+00:00"
        conn.execute(
            "INSERT INTO discovery_runs (id, project_id, status, started_at, created_at) "
            "VALUES ('d-run-1', 'default', 'running', ?, ?)",
            (now, now),
        )
        # Different project running simultaneously is allowed
        conn.execute(
            "INSERT INTO discovery_runs (id, project_id, status, started_at, created_at) "
            "VALUES ('d-run-other', 'alpha', 'running', ?, ?)",
            (now, now),
        )
        # Terminal runs for same project are allowed
        conn.execute(
            "INSERT INTO discovery_runs (id, project_id, status, started_at, created_at) "
            "VALUES ('d-run-done', 'default', 'completed', ?, ?)",
            (now, now),
        )
        # Second running run on same project violates partial unique index
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_runs (id, project_id, status, started_at, created_at) "
                "VALUES ('d-run-2', 'default', 'running', ?, ?)",
                (now, now),
            )

    command.downgrade(cfg, "0040")
    with sqlite3.connect(db_path) as conn:
        tables_after = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "discovery_runs" not in tables_after
        assert "discovery_candidates" not in tables_after


def test_stage1_three_path_recall_and_exclusions():
    from semantica_dataagent import fk_probing
    from semantica_dataagent.query_history_miner import JoinPairSignal

    tables = [
        {
            "database": "shop",
            "name": "dwd_orders",
            "fields": [
                {"name": "order_id", "type": "bigint", "partition": False},
                {"name": "user_id", "type": "bigint", "partition": False},
                {"name": "region_tag", "type": "varchar(32)", "partition": False},
                {"name": "store_id", "type": "bigint", "partition": False},
                {"name": "bad_type_id", "type": "varchar(16)", "partition": False},
                {"name": "dt", "type": "varchar(16)", "partition": True},
            ],
            "relationships": [
                {
                    "name": "existing_store_fk",
                    "from_table": "shop.dwd_orders",
                    "from_columns": ["store_id"],
                    "to_table": "shop.dim_store",
                    "to_columns": ["id"],
                }
            ],
        },
        {
            "database": "shop",
            "name": "dim_user",
            "fields": [
                {"name": "id", "type": "bigint", "partition": False},
                {"name": "dt", "type": "varchar(16)", "partition": True},
            ],
            "relationships": [],
        },
        {
            "database": "shop",
            "name": "dim_store",
            "fields": [
                {"name": "id", "type": "bigint", "partition": False},
            ],
            "relationships": [],
        },
        {
            "database": "shop",
            "name": "dim_geo",
            "fields": [
                {"name": "geo_key", "type": "varchar(32)", "partition": False},
                {"name": "bad_type_id", "type": "bigint", "partition": False},
            ],
            "relationships": [],
        },
    ]

    join_pairs = [
        # Path 1 valid join
        JoinPairSignal(
            from_table="shop.dwd_orders",
            from_catalog=None,
            from_database="shop",
            from_table_name="dwd_orders",
            from_columns=["user_id"],
            to_table="shop.dim_user",
            to_catalog=None,
            to_database="shop",
            to_table_name="dim_user",
            to_columns=["id"],
            execution_count=10,
            distinct_callers=3,
        ),
        # Pure partition pair -> MUST be excluded
        JoinPairSignal(
            from_table="shop.dwd_orders",
            from_catalog=None,
            from_database="shop",
            from_table_name="dwd_orders",
            from_columns=["dt"],
            to_table="shop.dim_user",
            to_catalog=None,
            to_database="shop",
            to_table_name="dim_user",
            to_columns=["dt"],
            execution_count=50,
            distinct_callers=5,
        ),
        # Same-table same-column self-link -> MUST be excluded
        JoinPairSignal(
            from_table="shop.dwd_orders",
            from_catalog=None,
            from_database="shop",
            from_table_name="dwd_orders",
            from_columns=["order_id"],
            to_table="shop.dwd_orders",
            to_catalog=None,
            to_database="shop",
            to_table_name="dwd_orders",
            to_columns=["order_id"],
            execution_count=20,
            distinct_callers=2,
        ),
    ]

    # Path 3 dimension overlap (region_tag <-> geo_key)
    dim_map = {
        ("shop", "dwd_orders", "region_tag"): ["CN_BJ", "CN_SH", "CN_SZ"],
        ("shop", "dim_geo", "geo_key"): ["CN_BJ", "CN_SH", "CN_SZ", "CN_HZ"],
    }
    candidates = fk_probing.recall_fk_candidates(
        tables=tables,
        join_pairs=join_pairs,
        max_fk_candidates=10,
        dimension_values_loader=lambda d, t, f: dim_map.get((d, t, f), []),
    )

    pairs = {
        (c.from_table, tuple(c.from_columns), c.to_table, tuple(c.to_columns)): c
        for c in candidates
    }
    # Corroborated Path 1 + Path 2: dwd_orders.user_id -> dim_user.id
    user_fk = pairs[("shop.dwd_orders", ("user_id",), "shop.dim_user", ("id",))]
    assert "query_history" in user_fk.recall_sources
    assert "name_type_similarity" in user_fk.recall_sources
    # Path 3 dimension overlap: dwd_orders.region_tag -> dim_geo.geo_key
    geo_fk = pairs[("shop.dwd_orders", ("region_tag",), "shop.dim_geo", ("geo_key",))]
    assert "dimension_overlap" in geo_fk.recall_sources
    # Excluded: existing RELATES_TO (store_id -> dim_store.id), incompatible types (bad_type_id), partition (dt), self-link
    assert all(
        "store_id" not in c.from_columns
        and "bad_type_id" not in c.from_columns
        and "dt" not in c.from_columns
        and c.from_table != c.to_table
        for c in candidates
    )

    # Top-K cap and max_fk_candidates=0
    assert (
        len(
            fk_probing.recall_fk_candidates(
                tables=tables, join_pairs=join_pairs, max_fk_candidates=1
            )
        )
        == 1
    )
    assert (
        fk_probing.recall_fk_candidates(
            tables=tables, join_pairs=join_pairs, max_fk_candidates=0
        )
        == []
    )


def test_stage2_fail_closed_skipped_no_partition_never_scans_business_table(
    env, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import fk_probing, knowledge
    from semantica_dataagent.dtb_ai_gateway import DtbAiGatewayExecutor

    _, _, owner_id, _, _ = env
    _save_personal_token(owner_id, "tok-no-part")
    _enable_dtb_executor()

    executed_sqls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/async-execute":
            body = json.loads(request.read().decode("utf-8"))
            executed_sqls.append(body.get("sql", ""))
            return httpx.Response(
                200,
                json={"data": {"queryKey": f"qk-{len(executed_sqls)}"}},
            )
        # Partition metadata query returns empty rows (no partition resolvable!)
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "200",
                    "data": {
                        "progress": 1.0,
                        "columns": [{"name": "partition_name", "type": "string"}],
                        "data": [],
                    },
                }
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"],
            tok,
            client=async_client,
            retry_delays=(),
            poll_interval=0,
        ),
    )
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args, **kwargs: [])

    tables = _sample_tables()
    cand = fk_probing.FkCandidate(
        from_table="shop.dwd_orders",
        from_catalog=None,
        from_database="shop",
        from_table_name="dwd_orders",
        from_columns=["user_id"],
        to_table="shop.dim_user",
        to_catalog=None,
        to_database="shop",
        to_table_name="dim_user",
        to_columns=["id"],
        recall_sources=["name_type_similarity"],
        recall_score=0.9,
    )
    outcomes = asyncio.run(
        fk_probing.probe_fk_candidates(
            candidates=[cand],
            tables=tables,
            owner_id=owner_id,
            project_id="default",
            sample_limit=500,
            min_inclusion_ratio=0.8,
        )
    )
    assert len(outcomes) == 1
    assert outcomes[0].decision == "skipped"
    assert outcomes[0].probing_status == "skipped_no_partition"
    assert outcomes[0].reason == "skipped_no_partition"
    # Only metadata_partition lookup ran; ZERO queries touched business tables dwd_orders or dim_user!
    assert executed_sqls
    assert all("metadata_partition" in sql for sql in executed_sqls), executed_sqls
    assert not any("FROM `shop`.`dwd_orders`" in sql for sql in executed_sqls)


def test_e2e_discovery_run_forced_pending_zero_leak_approve_join_and_revert(
    env, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import (
        canonical,
        knowledge,
        knowledge_synthesizer,
        semantic_overrides,
        sql_validation,
    )
    from semantica_dataagent.db import ProjectSetting, Provider, QueryAudit, sessions
    from semantica_dataagent.dtb_ai_gateway import DtbAiGatewayExecutor

    client, admin_token, owner_id, db_path, source_dir = env
    _save_personal_token(owner_id, "personal-dtb-secret-token-e2e")
    _enable_dtb_executor()

    # Disable auto-review switch to prove source="discovery" STILL forces status="pending"!
    with sessions().begin() as db:
        db.add(
            ProjectSetting(
                project_id="default", key="correction_require_review", value="false"
            )
        )
        db.add(
            Provider(
                id="prov-e2e",
                name="DiscoveryE2E",
                type="openai-completions",
                base_url="http://llm-e2e.local/v1",
                models=json.dumps(["gpt-e2e"]),
            )
        )

    tables = _sample_tables()
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: tables)
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args, **kwargs: [])
    monkeypatch.setattr(knowledge, "search", lambda *args, **kwargs: [])

    # Synthetic sentinel fact values that MUST NEVER appear in SQLite, LLM prompt, or source directory!
    sentinel_fact = "SENTINEL_FACT_VALUE_8839201"
    sentinel_email = "vip_customer@secret-corp.example"
    sentinel_phone = "13912345678"

    llm_payloads: list[dict] = []

    def fake_llm_post(url, headers=None, json=None, timeout=None, trust_env=False):
        import json as _json

        llm_payloads.append(json)
        content = _json.dumps(
            {
                "proposals": [
                    {
                        "route": "override",
                        "target": "metric:orders_gmv",
                        "reason": "汇总历史高频有效订单成交额指标口径",
                        "structured_patch": {
                            "version": 1,
                            "metrics": {
                                "orders_gmv": {
                                    "name": "订单成交额",
                                    "description": "统计历史订单金额汇总",
                                    "expression": "SUM(amount)",
                                    "dialect": "doris",
                                    "tables": ["shop.dwd_orders"],
                                }
                            },
                        },
                    },
                    {
                        "route": "new_document",
                        "new_doc_path": "knowledge/guides/orders_user_query.md",
                        "reason": "沉淀订单与用户关联查询指南",
                        "patch": "# 订单用户查询指南\n\n查询 `shop.dwd_orders` 时可按 `user_id = id` 关联 `shop.dim_user`。\n",
                    },
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(knowledge_synthesizer.httpx, "post", fake_llm_post)

    history_rows = [
        [
            f"SELECT SUM(o.amount) AS gmv FROM shop.dwd_orders o JOIN shop.dim_user u ON o.user_id = u.id WHERE o.dt = '2026-11-18' AND o.status = '{sentinel_fact}' AND u.user_name != '{sentinel_email}' -- phone {sentinel_phone}",
            "EOF",
            "2026-11-18 10:00:00",
            "shop",
            "analyst_1",
        ],
        [
            f"SELECT SUM(o.amount) AS gmv FROM shop.dwd_orders o JOIN shop.dim_user u ON o.user_id = u.id WHERE o.dt = '2026-11-18' AND o.status = '{sentinel_fact}' AND u.user_name != '{sentinel_email}'",
            "EOF",
            "2026-11-18 11:00:00",
            "shop",
            "analyst_2",
        ],
        [
            f"SELECT SUM(o.amount) AS gmv FROM shop.dwd_orders o JOIN shop.dim_user u ON o.user_id = u.id WHERE o.dt = '2026-11-18' AND o.status = '{sentinel_fact}' AND u.user_name != '{sentinel_email}'",
            "EOF",
            "2026-11-18 12:00:00",
            "shop",
            "analyst_3",
        ],
    ]

    sql_by_key: dict[str, str] = {}
    probe_sqls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/async-execute":
            body = json.loads(request.read().decode("utf-8"))
            sql = body.get("sql", "")
            qk = f"qk-{len(sql_by_key) + 1}"
            sql_by_key[qk] = sql
            return httpx.Response(200, json={"data": {"queryKey": qk}})
        if request.url.path.startswith("/result/"):
            qk = request.url.path.rsplit("/", 1)[-1]
            sql = sql_by_key[qk]
            if "audit_log" in sql:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "status": "200",
                            "data": {
                                "progress": 1.0,
                                "columns": [
                                    {"name": "sql_text", "type": "string"},
                                    {"name": "exec_status", "type": "string"},
                                    {"name": "exec_time", "type": "string"},
                                    {"name": "database_name", "type": "string"},
                                    {"name": "caller", "type": "string"},
                                ],
                                "data": history_rows,
                            },
                        }
                    },
                )
            if "metadata_partition" in sql:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "status": "200",
                            "data": {
                                "progress": 1.0,
                                "columns": [
                                    {"name": "partition_name", "type": "string"}
                                ],
                                "data": [["dt=2026-11-18"]],
                            },
                        }
                    },
                )
            probe_sqls.append(sql)
            if "sample_distinct_count" in sql:
                # High inclusion for user_id->id (95/100=0.95), low for any other pair
                if "dim_user" in sql:
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "status": "200",
                                "data": {
                                    "progress": 1.0,
                                    "columns": [
                                        {
                                            "name": "sample_distinct_count",
                                            "type": "bigint",
                                        },
                                        {"name": "matched_count", "type": "bigint"},
                                    ],
                                    "data": [[100, 95]],
                                },
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "status": "200",
                            "data": {
                                "progress": 1.0,
                                "columns": [
                                    {
                                        "name": "sample_distinct_count",
                                        "type": "bigint",
                                    },
                                    {"name": "matched_count", "type": "bigint"},
                                ],
                                "data": [[100, 20]],
                            },
                        }
                    },
                )
            # Step 2: SELECT 1 ... INNER JOIN ... LIMIT 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [{"name": "matched", "type": "int"}],
                            "data": [[1]],
                        },
                    }
                },
            )
        raise AssertionError(f"Unexpected path {request.url.path}")

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"],
            tok,
            client=async_client,
            retry_delays=(),
            poll_interval=0,
        ),
    )

    # Trigger discovery run via REST with enable_fk_probing=True
    start = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={
            "mine_query_history": True,
            "enable_fk_probing": True,
            "generate_eval_drafts": False,
        },
    )
    assert start.status_code == 202, start.text
    run_id = start.json()["id"]

    detail = _wait_run_terminal(client, admin_token, run_id)
    assert detail["status"] == "completed", detail
    assert detail["enable_fk_probing"] is True
    assert detail["queries_fetched"] == 3
    assert detail["templates_clustered"] == 1
    assert detail["templates_synthesized"] == 1
    assert detail["candidates_recalled"] >= 1
    assert detail["candidates_passed"] >= 1
    assert detail["fk_candidates_probed"] >= 1
    assert detail["fk_candidates_verified"] >= 1
    assert detail["proposals_created"] >= 3
    assert detail["eval_drafts_created"] == 0
    assert detail["llm_used"] is True
    assert detail["llm_error"] is None

    # Verify LLM prompt contained zero raw fact literals/PII/secrets
    assert len(llm_payloads) == 1
    llm_blob = json.dumps(llm_payloads[0], ensure_ascii=False)
    assert sentinel_fact not in llm_blob
    assert sentinel_email not in llm_blob
    assert sentinel_phone not in llm_blob
    assert "personal-dtb-secret-token-e2e" not in llm_blob

    # Verify probe SQLs enforced partition pruning on partitioned tables & LIMIT on unpartitioned tables
    assert any("2026-11-18" in sql and "LIMIT" in sql for sql in probe_sqls)

    # Verify QueryAudit recorded history + partition + probe queries
    with sessions()() as db:
        audits = db.query(QueryAudit).filter(QueryAudit.owner_id == owner_id).all()
        assert any(
            a.kind == "discovery_history" and a.status == "succeeded" for a in audits
        )
        assert any(
            a.kind == "discovery_probe" and a.status == "succeeded" for a in audits
        )

    # Verify all generated proposals are source="discovery" and FORCED status="pending"
    props_resp = client.get("/api/v1/corrections", headers=_headers(admin_token))
    assert props_resp.status_code == 200, props_resp.text
    props = props_resp.json()
    assert len(props) == detail["proposals_created"]
    assert all(p["source"] == "discovery" and p["status"] == "pending" for p in props)

    rel_prop = next(p for p in props if p["target"].startswith("relationship:"))
    assert rel_prop["evidence"]["source_type"] == "discovery"
    ev = rel_prop["gate_report"]["discovery_evidence"]
    assert ev["overlap_ratio"] == 0.95
    assert ev["inclusion_rate"] == 0.95
    assert ev["join_matched"] is True
    assert ev["join_hit"] is True
    assert ev["sample_distinct_count"] == 100
    assert ev["matched_count"] == 95
    assert ev["query_history_frequency"] == 3
    assert ev["distinct_callers"] == 3
    assert ev["probing_status"] == "passed"
    assert ev["probe_partitions"]["shop.dwd_orders"] == {"dt": "2026-11-18"}
    assert ev["probed_partition"]["shop.dwd_orders"] == {"dt": "2026-11-18"}

    # ZERO FACT-ROW LEAKAGE ASSERTION across entire SQLite DB & source directory!
    with sqlite3.connect(db_path) as conn:
        for (tbl_name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            for row in conn.execute(f'SELECT * FROM "{tbl_name}"').fetchall():
                blob = " ".join(str(cell) for cell in row if cell is not None)
                assert sentinel_fact not in blob, f"Leaked fact in {tbl_name}: {blob}"
                assert sentinel_email not in blob, f"Leaked email in {tbl_name}: {blob}"
                assert sentinel_phone not in blob, f"Leaked phone in {tbl_name}: {blob}"
                assert "personal-dtb-secret-token-e2e" not in blob

    for path in source_dir.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert sentinel_fact not in text
            assert sentinel_email not in text
            assert sentinel_phone not in text

    # Approve relationship proposal (with edited description to verify discovery_evidence preserved!)
    edited_patch = json.loads(json.dumps(rel_prop["structured_patch"]))
    rel_key = next(iter(edited_patch["relationships"]))
    edited_patch["relationships"][rel_key]["description"] = "管理员核验通过的用户外键"
    approve_resp = client.post(
        f"/api/v1/corrections/{rel_prop['id']}/approve",
        headers=_headers(admin_token),
        json={"structured_patch": edited_patch},
    )
    assert approve_resp.status_code == 200, approve_resp.text
    approved = approve_resp.json()
    assert approved["status"] == "approved"
    assert approved["commit_sha"]
    assert approved["gate_report"]["discovery_evidence"]["overlap_ratio"] == 0.95

    override_file = source_dir / "overrides" / "semantic_overrides.yaml"
    assert override_file.is_file()
    loaded_yaml = yaml.safe_load(override_file.read_text(encoding="utf-8"))
    assert rel_key in loaded_yaml["relationships"]

    # Overlay overrides onto canonical records and verify find_join_path & validate_sql!
    base_records = [
        canonical.Record(
            kind="table",
            data={"database": t["database"], "name": t["name"]},
            source_file="t.yaml",
        )
        for t in tables
    ] + [
        canonical.Record(
            kind="field",
            data={"database": t["database"], "table": t["name"], **f},
            source_file="t.yaml",
        )
        for t in tables
        for f in t["fields"]
    ]
    applied = semantic_overrides.apply_overrides_detailed(
        base_records, "default", source_dir=source_dir
    )
    rel_records = [r.data for r in applied.records if r.kind == "relationship"]
    assert len(rel_records) == 1

    snapshot = sql_validation.Snapshot(
        tables=tables, edges=[], documents=[], relationships=rel_records
    )
    jp = sql_validation.join_path(["shop.dwd_orders", "shop.dim_user"], snapshot)
    assert jp["status"] == "found"
    assert jp["strength"] == "strong"
    assert any(
        ev_item["type"] == "relationship"
        for step in jp["steps"]
        for ev_item in step["evidence"]
    )
    val = sql_validation.check_sql(
        "SELECT o.order_id, u.user_name FROM shop.dwd_orders o JOIN shop.dim_user u ON o.user_id = u.id WHERE o.dt = '2026-11-18' AND u.dt = '2026-11-18'",
        "doris",
        snapshot,
        partition_tables=tables,
    )
    assert val["status"] == "pass", val

    # Git revert cleanly rolls back semantic_overrides.yaml
    revert_resp = client.post(
        f"/api/v1/corrections/{rel_prop['id']}/revert",
        headers=_headers(admin_token),
    )
    assert revert_resp.status_code == 200, revert_resp.text
    reverted = revert_resp.json()
    assert reverted["status"] == "reverted"
    assert reverted["revert_commit_sha"]
    assert not override_file.exists()

    # Re-running discovery deduplicates still-pending metric & document proposals
    start2 = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={"mine_query_history": True, "enable_fk_probing": False},
    )
    assert start2.status_code == 202, start2.text
    detail2 = _wait_run_terminal(client, admin_token, start2.json()["id"])
    assert detail2["status"] == "completed"
    assert detail2["enable_fk_probing"] is False
    assert detail2["proposals_created"] == 0
    assert detail2["duplicates_skipped"] >= 2
    assert any(c["decision"] == "deduplicated" for c in detail2["candidates"])


def test_rest_guard_status_codes_and_spoofing_rejection(env, monkeypatch):
    from semantica_dataagent import corrections, discovery_runner, knowledge
    from semantica_dataagent.db import DiscoveryRun, sessions

    client, admin_token, owner_id, _, _ = env
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: _sample_tables())
    monkeypatch.setattr(
        discovery_runner, "execute_discovery_run", lambda run_id, **kwargs: None
    )

    # 1. Missing personal DTB token -> 400
    r400 = client.post("/api/v1/discovery/runs", headers=_headers(admin_token))
    assert r400.status_code == 400, r400.text

    # 2. Personal token present, executor disabled -> 503
    _save_personal_token(owner_id, "tok-guard")
    r503 = client.post("/api/v1/discovery/runs", headers=_headers(admin_token))
    assert r503.status_code == 503, r503.text

    # Enable executor
    _enable_dtb_executor()

    # 3. Non-admin member -> 403
    mem_resp = client.post(
        "/api/v1/tokens",
        headers=_headers(admin_token),
        json={
            "name": "member-user",
            "role": "user",
            "projects": [{"project_id": "default", "role": "member"}],
        },
    )
    assert mem_resp.status_code == 201
    mem_token = mem_resp.json()["token"]
    assert (
        client.post("/api/v1/discovery/runs", headers=_headers(mem_token)).status_code
        == 403
    )

    # 4. Document project -> 422
    doc_proj = client.post(
        "/api/v1/projects",
        headers=_headers(admin_token),
        json={"name": "Docs Only", "type": "document"},
    ).json()["id"]
    r_doc = client.post(
        "/api/v1/discovery/runs",
        params={"project": doc_proj},
        headers=_headers(admin_token),
    )
    assert r_doc.status_code == 422, r_doc.text

    # 5. Archived warehouse project -> 409
    wh_proj = client.post(
        "/api/v1/projects",
        headers=_headers(admin_token),
        json={"name": "Archived WH", "type": "warehouse"},
    ).json()["id"]
    assert (
        client.post(
            f"/api/v1/projects/{wh_proj}/archive", headers=_headers(admin_token)
        ).status_code
        == 200
    )
    r_arch = client.post(
        "/api/v1/discovery/runs",
        params={"project": wh_proj},
        headers=_headers(admin_token),
    )
    assert r_arch.status_code == 409, r_arch.text

    # 6. Out-of-range override / all flags disabled -> 422
    assert (
        client.post(
            "/api/v1/discovery/runs",
            headers=_headers(admin_token),
            json={"probe_concurrency": 5},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/discovery/runs",
            headers=_headers(admin_token),
            json={
                "mine_query_history": False,
                "probe_relationships": False,
                "seed_eval_drafts": False,
            },
        ).status_code
        == 422
    )

    # 7. Concurrent running run -> 409
    with sessions().begin() as db:
        db.add(
            DiscoveryRun(
                id="already-running",
                project_id="default",
                status="running",
                started_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
    r_conflict = client.post("/api/v1/discovery/runs", headers=_headers(admin_token))
    assert r_conflict.status_code == 409, r_conflict.text

    # 8. Cross-project / nonexistent run detail -> 404
    assert (
        client.get(
            "/api/v1/discovery/runs/already-running",
            params={"project": wh_proj},
            headers=_headers(admin_token),
        ).status_code
        == 404
    )

    # 9. External REST spoofing source="discovery" -> 422
    spoof = client.post(
        "/api/v1/corrections",
        headers=_headers(admin_token),
        json={
            "target": "table:shop.dwd_orders",
            "patch": "spoofed",
            "reason": "spoof attempt",
            "source": "discovery",
        },
    )
    assert spoof.status_code == 422, spoof.text

    # Direct MCP helper spoofing -> ValueError
    with pytest.raises(ValueError, match="MCP corrections must use source='mcp'"):
        corrections.propose_correction(
            "table:shop.dwd_orders",
            "x",
            "y",
            source="discovery",  # type: ignore[arg-type]
        )


def test_mid_run_401_aborts_immediately_and_single_candidate_timeout_degrades_partial(
    env, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.dtb_ai_gateway import DtbAiGatewayExecutor

    client, admin_token, owner_id, _, _ = env
    secret_tok = "tok-midrun-expiry-98765"
    _save_personal_token(owner_id, secret_tok)
    _enable_dtb_executor()

    # Build 3 tables so Stage 1 recalls 2 distinct FK candidates
    tables = [
        {
            "database": "shop",
            "name": "dwd_orders",
            "fields": [
                {"name": "user_id", "type": "bigint", "partition": False},
                {"name": "product_id", "type": "bigint", "partition": False},
            ],
            "relationships": [],
        },
        {
            "database": "shop",
            "name": "dim_user",
            "fields": [{"name": "id", "type": "bigint", "partition": False}],
            "relationships": [],
        },
        {
            "database": "shop",
            "name": "dim_product",
            "fields": [{"name": "id", "type": "bigint", "partition": False}],
            "relationships": [],
        },
    ]
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: tables)
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args, **kwargs: [])

    # Scenario A: 401 on first probe query -> immediate abort, second candidate NOT queried!
    probe_calls = {"count": 0}

    def handler_401(request: httpx.Request) -> httpx.Response:
        probe_calls["count"] += 1
        return httpx.Response(
            200,
            json={"data": {"status": "401", "message": f"expired token={secret_tok}"}},
        )

    c_401 = httpx.AsyncClient(transport=httpx.MockTransport(handler_401))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"], tok, client=c_401, retry_delays=(), poll_interval=0
        ),
    )

    r1 = client.post(
        "/api/v1/discovery/runs",
        headers=_headers(admin_token),
        json={
            "mine_query_history": False,
            "probe_relationships": True,
            "probe_concurrency": 1,
        },
    )
    assert r1.status_code == 202
    d1 = _wait_run_terminal(client, admin_token, r1.json()["id"])
    assert d1["status"] == "failed"
    assert d1["error"] and secret_tok not in d1["error"]
    assert (
        probe_calls["count"] == 1
    )  # aborted immediately without probing candidate #2!

    # Scenario B: Candidate #1 errors (502), Candidate #2 passes -> status="partial"
    sql_map: dict[str, str] = {}

    def handler_partial(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/async-execute":
            sql = json.loads(request.read().decode("utf-8")).get("sql", "")
            qk = f"p-{len(sql_map) + 1}"
            sql_map[qk] = sql
            return httpx.Response(200, json={"data": {"queryKey": qk}})
        qk = request.url.path.rsplit("/", 1)[-1]
        sql = sql_map[qk]
        if "dim_product" in sql:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "errorMessage": "sensitive platform stack",
                        },
                    }
                },
            )
        if "sample_distinct_count" in sql:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [
                                {"name": "sample_distinct_count", "type": "bigint"},
                                {"name": "matched_count", "type": "bigint"},
                            ],
                            "data": [[50, 48]],
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "200",
                    "data": {
                        "progress": 1.0,
                        "columns": [{"name": "matched", "type": "int"}],
                        "data": [[1]],
                    },
                }
            },
        )

    c_partial = httpx.AsyncClient(transport=httpx.MockTransport(handler_partial))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"], tok, client=c_partial, retry_delays=(), poll_interval=0
        ),
    )

    r2 = client.post(
        "/api/v1/discovery/runs",
        headers=_headers(admin_token),
        json={
            "mine_query_history": False,
            "probe_relationships": True,
            "probe_concurrency": 2,
        },
    )
    assert r2.status_code == 202
    d2 = _wait_run_terminal(client, admin_token, r2.json()["id"])
    assert d2["status"] == "partial", d2
    assert d2["candidates_passed"] == 1
    assert d2["candidates_failed"] == 1
    assert d2["proposals_created"] == 1
    assert "sensitive platform stack" not in (d2["error"] or "")
    decisions = {c["decision"] for c in d2["candidates"]}
    assert {"proposed", "failed"}.issubset(decisions)


def test_seed_eval_drafts_switch_validate_sql_and_dev_holdout_dedup(
    env, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import (
        eval_questions,
        knowledge,
        project_context,
        sql_validation,
    )
    from semantica_dataagent.dtb_ai_gateway import DtbAiGatewayExecutor

    client, admin_token, owner_id, _, _ = env
    _save_personal_token(owner_id, "tok-eval-seed")
    _enable_dtb_executor()

    tables = _sample_tables()
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: tables)
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args, **kwargs: [])
    # Mock validate_sql to use our in-memory snapshot
    snapshot = sql_validation.Snapshot(
        tables=tables, edges=[], documents=[], relationships=[]
    )
    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="doris", project_id=None: sql_validation.check_sql(
            sql, dialect, snapshot, partition_tables=tables
        ),
    )

    # Seed one existing holdout question matching query #2's scrubbed template!
    eval_questions.upsert_eval_question(
        "default",
        question="已存在的留出集问题",
        caliber="已确认留出集口径",
        reference_sql="SELECT COUNT(order_id) FROM shop.dwd_orders WHERE dt = '2026-11-10'",
        dialect="doris",
        split="holdout",
        answer_confirmed=True,
        source="manual",
        created_by=owner_id,
    )

    history_rows = [
        # Template 1: valid single-table query with partition filter -> should be seeded
        [
            "SELECT SUM(amount) AS gmv FROM shop.dwd_orders WHERE dt = '2026-11-18' AND status = 'paid'",
            "EOF",
            "2026-11-18 10:00:00",
            "shop",
            "u1",
        ],
        [
            "SELECT SUM(amount) AS gmv FROM shop.dwd_orders WHERE dt = '2026-11-18' AND status = 'paid'",
            "EOF",
            "2026-11-18 11:00:00",
            "shop",
            "u2",
        ],
        [
            "SELECT SUM(amount) AS gmv FROM shop.dwd_orders WHERE dt = '2026-11-18' AND status = 'paid'",
            "EOF",
            "2026-11-18 12:00:00",
            "shop",
            "u3",
        ],
        # Template 2: duplicates existing holdout question -> MUST be deduplicated!
        [
            "SELECT COUNT(order_id) FROM shop.dwd_orders WHERE dt = '2026-11-18'",
            "EOF",
            "2026-11-18 10:00:00",
            "shop",
            "u1",
        ],
        [
            "SELECT COUNT(order_id) FROM shop.dwd_orders WHERE dt = '2026-11-18'",
            "EOF",
            "2026-11-18 11:00:00",
            "shop",
            "u2",
        ],
        [
            "SELECT COUNT(order_id) FROM shop.dwd_orders WHERE dt = '2026-11-18'",
            "EOF",
            "2026-11-18 12:00:00",
            "shop",
            "u3",
        ],
        # Template 3: missing partition filter -> fails validate_sql -> MUST be rejected!
        [
            "SELECT SUM(amount) FROM shop.dwd_orders WHERE status = 'paid'",
            "EOF",
            "2026-11-18 10:00:00",
            "shop",
            "u1",
        ],
        [
            "SELECT SUM(amount) FROM shop.dwd_orders WHERE status = 'paid'",
            "EOF",
            "2026-11-18 11:00:00",
            "shop",
            "u2",
        ],
        [
            "SELECT SUM(amount) FROM shop.dwd_orders WHERE status = 'paid'",
            "EOF",
            "2026-11-18 12:00:00",
            "shop",
            "u3",
        ],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/async-execute":
            return httpx.Response(200, json={"data": {"queryKey": "qk-eval"}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "200",
                    "data": {
                        "progress": 1.0,
                        "columns": [
                            {"name": "sql_text", "type": "string"},
                            {"name": "exec_status", "type": "string"},
                            {"name": "exec_time", "type": "string"},
                            {"name": "database_name", "type": "string"},
                            {"name": "caller", "type": "string"},
                        ],
                        "data": history_rows,
                    },
                }
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"],
            tok,
            client=async_client,
            retry_delays=(),
            poll_interval=0,
        ),
    )

    resp = client.post(
        "/api/v1/discovery/runs",
        headers=_headers(admin_token),
        json={
            "mine_query_history": False,
            "probe_relationships": False,
            "seed_eval_drafts": True,
        },
    )
    assert resp.status_code == 202, resp.text
    detail = _wait_run_terminal(client, admin_token, resp.json()["id"])
    assert detail["status"] == "completed", detail
    assert detail["eval_drafts_created"] == 1

    eval_cands = [
        c for c in detail["candidates"] if c["candidate_kind"] == "eval_draft"
    ]
    decisions = {c["decision"] for c in eval_cands}
    assert decisions == {"proposed", "deduplicated", "rejected"}

    # Check isolated eval questions file & REST view
    eval_path = project_context.eval_questions_path("default")
    assert eval_path.is_file()
    admin_list = client.get(
        "/api/v1/eval-questions",
        params={"source": "discovery"},
        headers=_headers(admin_token),
    )
    assert admin_list.status_code == 200
    items = admin_list.json()
    assert len(items) == 1
    assert items[0]["answer_confirmed"] is False
    assert items[0]["split"] == "dev"
    assert items[0]["source"] == "discovery"
    assert "paid" not in (items[0]["reference_sql"] or "")

    # Non-admin member sees discovery unconfirmed draft redacted
    mem_tok = client.post(
        "/api/v1/tokens",
        headers=_headers(admin_token),
        json={
            "name": "eval-reader",
            "role": "user",
            "projects": [{"project_id": "default", "role": "member"}],
        },
    ).json()["token"]
    mem_list = client.get(
        "/api/v1/eval-questions",
        params={"source": "discovery"},
        headers=_headers(mem_tok),
    ).json()
    assert len(mem_list) == 1
    assert mem_list[0]["redacted"] is True
    assert mem_list[0]["reference_sql"] is None


def test_crash_recovery_in_independent_subprocess_and_project_deletion_cascade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "crash.db"
    projects_root = tmp_path / "projects"
    enc_key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", enc_key)
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")

    from semantica_dataagent.cli import initialize

    admin_token = initialize()

    # 1. Spawn an independent subprocess that inserts a running discovery run and crashes via os._exit(1)
    crash_script = """
import os
from datetime import UTC, datetime
from semantica_dataagent.db import DiscoveryRun, sessions

with sessions().begin() as db:
    db.add(
        DiscoveryRun(
            id="crashed-run-1",
            project_id="default",
            status="running",
            started_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
os._exit(1)
"""
    sub_env = {
        **os.environ,
        "DATAAGENT_DB_PATH": str(db_path),
        "DATAAGENT_ENCRYPTION_KEY": enc_key,
        "DATAAGENT_PROJECTS_ROOT": str(projects_root),
    }
    proc = subprocess.run(
        [sys.executable, "-c", crash_script], env=sub_env, check=False
    )
    assert proc.returncode == 1

    # 2. Boot app lifespan -> startup recovery marks crashed-run-1 as incomplete cleanly
    from semantica_dataagent import project_deletion
    from semantica_dataagent.app import app
    from semantica_dataagent.db import (
        DiscoveryCandidate,
        DiscoveryRun,
        ProjectSetting,
        sessions,
    )

    with TestClient(app) as client:
        recovered = client.get(
            "/api/v1/discovery/runs/crashed-run-1",
            headers=_headers(admin_token),
        )
        assert recovered.status_code == 200, recovered.text
        data = recovered.json()
        assert data["status"] == "incomplete"
        assert data["finished_at"] is not None
        assert "interrupted" in (data["error"] or "").lower()

        # 3. Project permanent deletion cascades discovery_candidates, discovery_runs, and discovery settings
        proj_id = client.post(
            "/api/v1/projects",
            headers=_headers(admin_token),
            json={"name": "Cascade Discovery Project", "type": "warehouse"},
        ).json()["id"]
        assert (
            client.put(
                f"/api/v1/projects/{proj_id}/discovery-config",
                headers=_headers(admin_token),
                json={"lookback_days": 14},
            ).status_code
            == 200
        )
        with sessions().begin() as db:
            db.add(
                DiscoveryRun(
                    id="cascade-run-1",
                    project_id=proj_id,
                    status="completed",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                )
            )
            db.add(
                DiscoveryCandidate(
                    id="cascade-cand-1",
                    run_id="cascade-run-1",
                    project_id=proj_id,
                    candidate_kind="relationship",
                    target="relationship:a:b:fk",
                    evidence_json="{}",
                    decision="proposed",
                    created_at=datetime.now(UTC),
                )
            )

        assert (
            client.post(
                f"/api/v1/projects/{proj_id}/archive", headers=_headers(admin_token)
            ).status_code
            == 200
        )
        # Skip Neo4j/Milvus in offline deletion test
        monkeypatch.setattr(project_deletion, "_delete_neo4j", lambda pid, cfg: None)
        monkeypatch.setattr(project_deletion, "_delete_milvus", lambda pid, cfg: None)
        del_resp = client.post(
            f"/api/v1/projects/{proj_id}/delete",
            headers=_headers(admin_token),
            json={"name": "Cascade Discovery Project"},
        )
        assert del_resp.status_code == 204, del_resp.text

        with sessions()() as db:
            assert (
                db.query(DiscoveryRun)
                .filter(DiscoveryRun.project_id == proj_id)
                .count()
                == 0
            )
            assert (
                db.query(DiscoveryCandidate)
                .filter(DiscoveryCandidate.project_id == proj_id)
                .count()
                == 0
            )
            assert (
                db.query(ProjectSetting)
                .filter(ProjectSetting.project_id == proj_id)
                .count()
                == 0
            )
            # Default project's recovered run remains untouched!
            assert db.get(DiscoveryRun, "crashed-run-1") is not None


def test_discovery_default_fk_probing_false_skips_probes_and_synthesizes_override_and_docs(
    env, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge, knowledge_synthesizer
    from semantica_dataagent.db import Provider, QueryAudit, sessions
    from semantica_dataagent.dtb_ai_gateway import DtbAiGatewayExecutor

    client, admin_token, owner_id, _, source_dir = env
    _save_personal_token(owner_id, "tok-default-no-fk")
    _enable_dtb_executor()

    # Seed existing Markdown doc and LLM provider
    (source_dir / "knowledge" / "metrics").mkdir(parents=True, exist_ok=True)
    (source_dir / "knowledge" / "metrics" / "gmv.md").write_text(
        "# GMV 口径\n\n订单总金额统计规则。\n", encoding="utf-8"
    )
    with sessions().begin() as db:
        db.add(
            Provider(
                id="prov-m4",
                name="M4Provider",
                type="openai-completions",
                base_url="http://llm-m4.local/v1",
                models=json.dumps(["gpt-m4", "gpt-m4-mini"]),
            )
        )

    tables = _sample_tables()
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: tables)
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args, **kwargs: [])
    monkeypatch.setattr(knowledge, "search", lambda *args, **kwargs: [])

    # 3 distinct templates above min_frequency=3 so top_templates=2 cleanly caps synthesis input
    history_rows = []
    for i in range(5):
        history_rows.append(
            [
                "SELECT SUM(amount) AS gmv FROM shop.dwd_orders WHERE dt = '2026-11-18' AND status = 'paid'",
                "EOF",
                "2026-11-18 10:00:00",
                "shop",
                f"u{i % 3}",
            ]
        )
    for i in range(4):
        history_rows.append(
            [
                "SELECT o.city_code, COUNT(o.order_id) AS cnt FROM shop.dwd_orders o JOIN shop.dim_city c ON o.city_code = c.code WHERE o.dt = '2026-11-18' GROUP BY o.city_code",
                "EOF",
                "2026-11-18 11:00:00",
                "shop",
                f"c{i % 2}",
            ]
        )
    for _ in range(3):
        history_rows.append(
            [
                "SELECT COUNT(DISTINCT user_id) AS uv FROM shop.dwd_orders WHERE dt = '2026-11-18'",
                "EOF",
                "2026-11-18 12:00:00",
                "shop",
                "u9",
            ]
        )

    executed_sqls: list[str] = []

    def dtb_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/async-execute":
            sql = json.loads(request.read().decode("utf-8")).get("sql", "")
            executed_sqls.append(sql)
            return httpx.Response(200, json={"data": {"queryKey": "qk-1"}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "200",
                    "data": {
                        "progress": 1.0,
                        "columns": [
                            {"name": "sql_text", "type": "string"},
                            {"name": "exec_status", "type": "string"},
                            {"name": "exec_time", "type": "string"},
                            {"name": "database_name", "type": "string"},
                            {"name": "caller", "type": "string"},
                        ],
                        "data": history_rows,
                    },
                }
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(dtb_handler))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"],
            tok,
            client=async_client,
            retry_delays=(),
            poll_interval=0,
        ),
    )

    llm_calls: list[dict] = []

    def llm_post(url, headers=None, json=None, timeout=None, trust_env=False):
        import json as _json

        llm_calls.append(json)
        content = _json.dumps(
            {
                "proposals": [
                    {
                        "route": "override",
                        "target": "field:shop.dwd_orders.status",
                        "reason": "补充订单状态字段业务过滤约束",
                        "structured_patch": {
                            "version": 1,
                            "fields": {
                                "shop.dwd_orders.status": {
                                    "database": "shop",
                                    "table": "dwd_orders",
                                    "name": "status",
                                    "instructions": "统计有效 GMV 时默认只取已支付状态",
                                }
                            },
                        },
                    },
                    {
                        "route": "existing_document",
                        "target": "document:metrics/gmv.md",
                        "reason": "合并更新已有 GMV 口径文档",
                        "patch": "# GMV 口径\n\n订单总金额统计规则。\n\n## 常用表\n\n使用 `shop.dwd_orders` 按 `dt` 分区过滤。\n",
                    },
                    {
                        "route": "new_document",
                        "new_doc_path": "knowledge/guides/city_order_guide.md",
                        "reason": "新建跨表城市订单查询指南文档",
                        "patch": "# 城市订单查询指南\n\n通过 `city_code = code` 关联 `shop.dwd_orders` 与 `shop.dim_city`。\n",
                    },
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(knowledge_synthesizer.httpx, "post", llm_post)

    # Trigger via project-scoped path WITHOUT enable_fk_probing (must default to False!)
    resp = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={
            "top_templates": 2,
            "provider_id": "prov-m4",
            "model": "gpt-m4-mini",
        },
    )
    assert resp.status_code == 202, resp.text
    created_run = resp.json()
    assert created_run["enable_fk_probing"] is False
    assert created_run["top_templates"] == 2

    detail = _wait_run_terminal(client, admin_token, created_run["id"])
    assert detail["status"] == "completed", detail
    assert detail["enable_fk_probing"] is False
    assert detail["templates_clustered"] == 3
    assert detail["templates_synthesized"] == 2
    assert detail["fk_candidates_probed"] == 0
    assert detail["fk_candidates_verified"] == 0
    assert detail["proposals_created"] == 3
    assert detail["duplicates_skipped"] == 0
    assert detail["llm_used"] is True
    assert detail["llm_error"] is None
    assert len(detail["proposal_ids"]) == 3

    # Assert ZERO FK probe SQLs or metadata_partition queries executed (only 1 history SQL!)
    assert len(executed_sqls) == 1
    assert "audit_log" in executed_sqls[0]
    assert not any(
        "metadata_partition" in s or "sample_distinct_count" in s for s in executed_sqls
    )

    with sessions()() as db:
        audits = db.query(QueryAudit).filter(QueryAudit.owner_id == owner_id).all()
        assert [a.kind for a in audits] == ["discovery_history"]
        assert audits[0].status == "succeeded"

    # Verify LLM context respected top_templates=2 and carried scrubbed templates
    assert len(llm_calls) == 1
    assert llm_calls[0]["model"] == "gpt-m4-mini"
    user_ctx = json.loads(llm_calls[0]["messages"][1]["content"])
    assert user_ctx["source_type"] == "discovery"
    assert len(user_ctx["evidence"]["sql_clusters"]) == 2
    assert user_ctx["evidence"]["sql_clusters"][0]["execution_count"] == 5
    assert user_ctx["evidence"]["sql_clusters"][1]["execution_count"] == 4
    assert user_ctx["evidence"]["fk_probe_evidence"] == []
    assert "paid" not in json.dumps(user_ctx, ensure_ascii=False)

    # Verify list & detail views on project-scoped endpoints expose stats
    listed = client.get(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["templates_clustered"] == 3
    assert listed.json()[0]["proposals_created"] == 3

    # Verify all 3 proposals are pending, routed per ADR-0013, and carry unified evidence
    props = client.get("/api/v1/corrections", headers=_headers(admin_token)).json()
    assert len(props) == 3
    assert all(p["source"] == "discovery" and p["status"] == "pending" for p in props)
    by_target = {p["target"]: p for p in props}

    assert "field:shop.dwd_orders.status" in by_target
    assert by_target["field:shop.dwd_orders.status"]["target_kind"] == "override"
    assert (
        by_target["field:shop.dwd_orders.status"]["evidence"]["source_type"]
        == "discovery"
    )
    assert by_target["field:shop.dwd_orders.status"]["evidence"]["trigger_evidence"][
        "sql_clusters"
    ]

    existing_doc_prop = by_target.get("document:metrics/gmv.md") or by_target.get(
        "document:knowledge/metrics/gmv.md"
    )
    assert existing_doc_prop is not None, list(by_target)
    assert existing_doc_prop["target_kind"] == "document"
    assert existing_doc_prop["is_new_file"] is False
    assert "+使用 `shop.dwd_orders`" in existing_doc_prop["diff"]

    new_doc_prop = by_target["document:knowledge/guides/city_order_guide.md"]
    assert new_doc_prop["target_kind"] == "document"
    assert new_doc_prop["is_new_file"] is True
    assert "--- /dev/null" in new_doc_prop["diff"]

    # Re-run deduplicates all 3 still-pending proposals
    resp2 = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={"top_templates": 2, "provider_id": "prov-m4", "model": "gpt-m4-mini"},
    )
    assert resp2.status_code == 202
    detail2 = _wait_run_terminal(client, admin_token, resp2.json()["id"])
    assert detail2["status"] == "completed"
    assert detail2["proposals_created"] == 0
    assert detail2["duplicates_skipped"] == 3


def test_discovery_llm_unavailable_explicit_error_and_fk_probing_preservation(
    env, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge, knowledge_synthesizer
    from semantica_dataagent.db import Provider, sessions
    from semantica_dataagent.dtb_ai_gateway import DtbAiGatewayExecutor

    client, admin_token, owner_id, _, _ = env
    _save_personal_token(owner_id, "tok-llm-fail")
    _enable_dtb_executor()

    tables = _sample_tables()
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: tables)
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args, **kwargs: [])
    monkeypatch.setattr(knowledge, "search", lambda *args, **kwargs: [])

    history_rows = [
        [
            "SELECT SUM(o.amount) AS gmv FROM shop.dwd_orders o JOIN shop.dim_user u ON o.user_id = u.id WHERE o.dt = '2026-11-18' AND o.status = 'paid'",
            "EOF",
            "2026-11-18 10:00:00",
            "shop",
            f"u{i}",
        ]
        for i in range(3)
    ]

    sql_by_key: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/async-execute":
            sql = json.loads(request.read().decode("utf-8")).get("sql", "")
            qk = f"qk-{len(sql_by_key) + 1}"
            sql_by_key[qk] = sql
            return httpx.Response(200, json={"data": {"queryKey": qk}})
        qk = request.url.path.rsplit("/", 1)[-1]
        sql = sql_by_key[qk]
        if "audit_log" in sql:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [
                                {"name": "sql_text", "type": "string"},
                                {"name": "exec_status", "type": "string"},
                                {"name": "exec_time", "type": "string"},
                                {"name": "database_name", "type": "string"},
                                {"name": "caller", "type": "string"},
                            ],
                            "data": history_rows,
                        },
                    }
                },
            )
        if "metadata_partition" in sql:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [{"name": "partition_name", "type": "string"}],
                            "data": [["dt=2026-11-18"]],
                        },
                    }
                },
            )
        if "sample_distinct_count" in sql:
            matched = 92 if "dim_user" in sql else 10
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [
                                {"name": "sample_distinct_count", "type": "bigint"},
                                {"name": "matched_count", "type": "bigint"},
                            ],
                            "data": [[100, matched]],
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "200",
                    "data": {
                        "progress": 1.0,
                        "columns": [{"name": "matched", "type": "int"}],
                        "data": [[1]],
                    },
                }
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"],
            tok,
            client=async_client,
            retry_delays=(),
            poll_interval=0,
        ),
    )

    # Case A: enable_fk_probing=False and NO LLM provider configured -> explicit failure, 0 fake proposals
    r_no_llm = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={"enable_fk_probing": False},
    )
    assert r_no_llm.status_code == 202
    d_no_llm = _wait_run_terminal(client, admin_token, r_no_llm.json()["id"])
    assert d_no_llm["status"] == "failed", d_no_llm
    assert d_no_llm["llm_used"] is False
    assert d_no_llm["llm_error"]
    assert d_no_llm["error"]
    assert d_no_llm["proposals_created"] == 0
    assert client.get("/api/v1/corrections", headers=_headers(admin_token)).json() == []

    # Case B: Provider configured, LLM returns HTTP 500, enable_fk_probing=True ->
    # records explicit LLM error/warning while preserving verified FK probe proposal!
    with sessions().begin() as db:
        db.add(
            Provider(
                id="prov-fail",
                name="FailingLLM",
                type="openai-completions",
                base_url="http://llm-fail.local/v1",
                models=json.dumps(["gpt-fail"]),
            )
        )

    monkeypatch.setattr(
        knowledge_synthesizer.httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            500, request=httpx.Request("POST", url), json={"error": "upstream down"}
        ),
    )

    r_fk_llm_fail = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={
            "enable_fk_probing": True,
            "provider_id": "prov-fail",
            "model": "gpt-fail",
        },
    )
    assert r_fk_llm_fail.status_code == 202
    d_fk = _wait_run_terminal(client, admin_token, r_fk_llm_fail.json()["id"])
    assert d_fk["status"] == "partial", d_fk
    assert d_fk["llm_used"] is False
    assert d_fk["llm_error"]
    assert d_fk["error"]
    assert d_fk["fk_candidates_verified"] == 1
    assert d_fk["proposals_created"] == 1

    props = client.get("/api/v1/corrections", headers=_headers(admin_token)).json()
    assert len(props) == 1
    assert props[0]["source"] == "discovery"
    assert props[0]["status"] == "pending"
    assert props[0]["target"].startswith("relationship:")
    assert props[0]["evidence"]["source_type"] == "discovery"
    assert props[0]["evidence"]["trigger_evidence"]["inclusion_rate"] == 0.92
    assert props[0]["evidence"]["trigger_evidence"]["join_hit"] is True
    assert props[0]["evidence"]["trigger_evidence"]["probed_partition"][
        "shop.dwd_orders"
    ] == {"dt": "2026-11-18"}


def test_project_path_discovery_runs_guards_and_trigger_validation(env, monkeypatch):
    from semantica_dataagent import discovery_runner, knowledge
    from semantica_dataagent.db import DiscoveryRun, Provider, sessions

    client, admin_token, owner_id, _, _ = env
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: _sample_tables())
    monkeypatch.setattr(
        discovery_runner, "execute_discovery_run", lambda run_id, **kwargs: None
    )

    with sessions().begin() as db:
        db.add(
            Provider(
                id="prov-val",
                name="ValProvider",
                type="openai-completions",
                base_url="http://llm-val.local/v1",
                models=json.dumps(["gpt-val"]),
            )
        )

    # Missing personal DTB token -> 400
    assert (
        client.post(
            "/api/v1/projects/default/discovery/runs",
            headers=_headers(admin_token),
        ).status_code
        == 400
    )

    _save_personal_token(owner_id, "tok-path-guard")
    _enable_dtb_executor()

    # Document project -> 422
    doc_proj = client.post(
        "/api/v1/projects",
        headers=_headers(admin_token),
        json={"name": "Doc Guard", "type": "document"},
    ).json()["id"]
    assert (
        client.post(
            f"/api/v1/projects/{doc_proj}/discovery/runs",
            headers=_headers(admin_token),
        ).status_code
        == 422
    )

    # Archived warehouse project -> 409 on POST, 200 on GET
    wh_proj = client.post(
        "/api/v1/projects",
        headers=_headers(admin_token),
        json={"name": "Arch WH", "type": "warehouse"},
    ).json()["id"]
    assert (
        client.post(
            f"/api/v1/projects/{wh_proj}/archive", headers=_headers(admin_token)
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/projects/{wh_proj}/discovery/runs",
            headers=_headers(admin_token),
        ).status_code
        == 409
    )
    assert (
        client.get(
            f"/api/v1/projects/{wh_proj}/discovery/runs",
            headers=_headers(admin_token),
        ).status_code
        == 200
    )

    # Validation checks on top_templates & provider/model -> 422
    for bad_body in (
        {"top_templates": 0},
        {"top_templates": 101},
        {"top_templates": True},
        {"provider_id": "missing-prov"},
        {"provider_id": "prov-val", "model": "unknown-model"},
        {"model": "gpt-val"},
    ):
        r = client.post(
            "/api/v1/projects/default/discovery/runs",
            headers=_headers(admin_token),
            json=bad_body,
        )
        assert r.status_code == 422, (bad_body, r.text)

    # Concurrent run -> 409
    with sessions().begin() as db:
        db.add(
            DiscoveryRun(
                id="path-running-1",
                project_id="default",
                status="running",
                started_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
    assert (
        client.post(
            "/api/v1/projects/default/discovery/runs",
            headers=_headers(admin_token),
        ).status_code
        == 409
    )


def test_verified_fk_not_dropped_when_llm_bundles_with_duplicate_override(
    env, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge, knowledge_synthesizer
    from semantica_dataagent.db import CorrectionProposal, Provider, sessions
    from semantica_dataagent.dtb_ai_gateway import DtbAiGatewayExecutor

    client, admin_token, owner_id, _, _ = env
    _save_personal_token(owner_id, "tok-fk-bundle")
    _enable_dtb_executor()

    tables = _sample_tables()
    monkeypatch.setattr(knowledge, "describe", lambda *args, **kwargs: tables)
    monkeypatch.setattr(knowledge, "dimension_values", lambda *args, **kwargs: [])
    monkeypatch.setattr(knowledge, "search", lambda *args, **kwargs: [])

    # Seed an existing pending table override on shop.dwd_orders and an LLM provider
    with sessions().begin() as db:
        db.add(
            CorrectionProposal(
                id="pend-tbl-orders",
                project_id="default",
                owner_id=owner_id,
                target="table:shop.dwd_orders",
                patch="version: 1\n",
                original="",
                diff="diff",
                reason="订单明细表说明",
                status="pending",
                source="manual",
                target_kind="override",
                structured_patch=json.dumps(
                    {
                        "version": 1,
                        "tables": {
                            "shop.dwd_orders": {
                                "database": "shop",
                                "name": "dwd_orders",
                                "comment": "订单明细表说明",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.add(
            Provider(
                id="prov-bundle",
                name="BundleProvider",
                type="openai-completions",
                base_url="http://llm-bundle.local/v1",
                models=json.dumps(["gpt-bundle"]),
            )
        )

    history_rows = [
        [
            "SELECT SUM(o.amount) AS gmv FROM shop.dwd_orders o JOIN shop.dim_user u ON o.user_id = u.id WHERE o.dt = '2026-11-18'",
            "EOF",
            "2026-11-18 10:00:00",
            "shop",
            f"u{i}",
        ]
        for i in range(3)
    ]

    sql_by_key: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/async-execute":
            sql = json.loads(request.read().decode("utf-8")).get("sql", "")
            qk = f"qk-{len(sql_by_key) + 1}"
            sql_by_key[qk] = sql
            return httpx.Response(200, json={"data": {"queryKey": qk}})
        qk = request.url.path.rsplit("/", 1)[-1]
        sql = sql_by_key[qk]
        if "audit_log" in sql:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [
                                {"name": "sql_text", "type": "string"},
                                {"name": "exec_status", "type": "string"},
                                {"name": "exec_time", "type": "string"},
                                {"name": "database_name", "type": "string"},
                                {"name": "caller", "type": "string"},
                            ],
                            "data": history_rows,
                        },
                    }
                },
            )
        if "metadata_partition" in sql:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [{"name": "partition_name", "type": "string"}],
                            "data": [["dt=2026-11-18"]],
                        },
                    }
                },
            )
        if "sample_distinct_count" in sql:
            matched = 95 if "dim_user" in sql else 10
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "200",
                        "data": {
                            "progress": 1.0,
                            "columns": [
                                {"name": "sample_distinct_count", "type": "bigint"},
                                {"name": "matched_count", "type": "bigint"},
                            ],
                            "data": [[100, matched]],
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "200",
                    "data": {
                        "progress": 1.0,
                        "columns": [{"name": "matched", "type": "int"}],
                        "data": [[1]],
                    },
                }
            },
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        DtbAiGatewayExecutor,
        "bind",
        lambda self, params, tok: DtbAiGatewayExecutor(
            params["base_url"],
            tok,
            client=async_client,
            retry_delays=(),
            poll_interval=0,
        ),
    )

    # LLM returns a combined patch bundling the duplicate table override with the new verified FK
    def llm_post(url, headers=None, json=None, timeout=None, trust_env=False):
        import json as _json

        content = _json.dumps(
            {
                "proposals": [
                    {
                        "route": "override",
                        "target": "table:shop.dwd_orders",
                        "reason": "补充订单表说明与关联外键",
                        "structured_patch": {
                            "version": 1,
                            "tables": {
                                "shop.dwd_orders": {
                                    "database": "shop",
                                    "name": "dwd_orders",
                                    "comment": "订单明细表说明",
                                }
                            },
                            "relationships": {
                                "fk_shop_dwd_orders__shop_dim_user__user_id": {
                                    "name": "fk_shop_dwd_orders__shop_dim_user__user_id",
                                    "from_database": "shop",
                                    "from_table": "dwd_orders",
                                    "from_columns": ["user_id"],
                                    "to_database": "shop",
                                    "to_table": "dim_user",
                                    "to_columns": ["id"],
                                }
                            },
                        },
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(knowledge_synthesizer.httpx, "post", llm_post)

    resp = client.post(
        "/api/v1/projects/default/discovery/runs",
        headers=_headers(admin_token),
        json={
            "enable_fk_probing": True,
            "provider_id": "prov-bundle",
            "model": "gpt-bundle",
        },
    )
    assert resp.status_code == 202, resp.text
    detail = _wait_run_terminal(client, admin_token, resp.json()["id"])
    assert detail["status"] == "completed", detail
    assert detail["fk_candidates_verified"] == 1
    assert detail["duplicates_skipped"] == 1
    assert detail["proposals_created"] == 1

    props = client.get("/api/v1/corrections", headers=_headers(admin_token)).json()
    rel_props = [
        p
        for p in props
        if p["source"] == "discovery" and p["target"].startswith("relationship:")
    ]
    assert len(rel_props) == 1
    assert rel_props[0]["status"] == "pending"
    assert rel_props[0]["evidence"]["trigger_evidence"]["inclusion_rate"] == 0.95
