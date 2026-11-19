"""Tests for controlled AST SQL builder, personal DTB token execution, and QueryAudit (#303)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import sqlglot
from cryptography.fernet import Fernet
from sqlglot import exp


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Token, digest, sessions

    admin_token = initialize()
    with sessions()() as db:
        owner_id = db.get(Token, digest(admin_token)).id
    return owner_id, admin_token


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
            "dtb",
            True,
            {
                "base_url": base_url,
                "row_limit": 5000,
                "download_limit": 20000,
                "timeout_seconds": timeout_seconds,
            },
        )


def test_build_query_history_sql_uses_ast_with_time_bound_and_limit():
    from semantica_dataagent import discovery_executor, project_context

    cfg = {
        **project_context.DISCOVERY_PRESETS["doris_audit_log"],
        "lookback_days": 7,
        "max_queries": 1500,
    }
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
    sql = discovery_executor.build_query_history_sql(
        cfg, now=now, database_filter=["shop", "dw"]
    )
    tree = sqlglot.parse_one(sql, read="doris")
    assert isinstance(tree, exp.Select)
    # Mandatory LIMIT
    limit_node = tree.args.get("limit")
    assert limit_node is not None
    assert int(limit_node.expression.this) == 1500
    # Mandatory time lower-bound predicate
    gtes = list(tree.find_all(exp.GTE))
    assert any(
        isinstance(g.this, exp.Column)
        and g.this.name == "time"
        and isinstance(g.expression, exp.Literal)
        and g.expression.this == "2026-11-12 12:00:00"
        for g in gtes
    ), sql
    # Status and database IN filters
    ins = list(tree.find_all(exp.In))
    assert len(ins) == 2, sql
    assert "__internal_schema" in sql and "audit_log" in sql


def test_build_query_history_sql_single_status_and_three_part_table():
    from semantica_dataagent import discovery_executor, project_context

    cfg = {
        **project_context.DISCOVERY_PRESETS["dtb_tracking_table"],
        "success_statuses": ["SUCCESS"],
        "caller_column": None,
        "dialect_column": None,
        "lookback_days": 3,
        "max_queries": 200,
    }
    sql = discovery_executor.build_query_history_sql(
        cfg, now=datetime(2026, 11, 19, 0, 0, 0, tzinfo=UTC)
    )
    tree = discovery_executor.ensure_readonly_select_sql(sql, require_limit=True)
    assert isinstance(tree, exp.Select)
    assert "mysql" in sql and "data_tool_box_emr" in sql and "query_history" in sql
    assert "`caller`" not in sql and "`dialect`" not in sql
    assert "2026-11-16 00:00:00" in sql


@pytest.mark.parametrize(
    "bad_overrides",
    [
        {"source_table": "audit_log; DROP TABLE users"},
        {"source_table": "a.b.c.d"},
        {"sql_column": "stmt FROM dual; --"},
        {"status_column": "state' OR '1'='1"},
        {"time_column": "time; SELECT 1"},
        {"database_column": "db name"},
        {"caller_column": "user()"},
        {"success_statuses": []},
        {"success_statuses": ["EOF'; DELETE FROM t--"]},
        {"lookback_days": 0},
        {"lookback_days": 91},
        {"max_queries": 10},
        {"max_queries": 99999},
    ],
)
def test_build_query_history_sql_rejects_injection_and_invalid_values(bad_overrides):
    from semantica_dataagent import discovery_executor, project_context

    cfg = {
        **project_context.DISCOVERY_PRESETS["doris_audit_log"],
        "lookback_days": 7,
        "max_queries": 500,
        **bad_overrides,
    }
    with pytest.raises(discovery_executor.DiscoverySqlValidationError) as exc_info:
        discovery_executor.build_query_history_sql(cfg)
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    "bad_sql",
    [
        "DROP TABLE shop.orders",
        "DELETE FROM shop.orders WHERE id = 1",
        "INSERT INTO shop.orders VALUES (1)",
        "SELECT 1 LIMIT 1; DROP TABLE shop.orders",
        "SELECT * FROM shop.orders",  # missing LIMIT
        "SELECT * FROM shop.orders LIMIT 0",
        "SELECT * FROM shop.orders INTO OUTFILE '/tmp/x' LIMIT 1",
    ],
)
def test_ensure_readonly_select_sql_blocks_unsafe_statements(bad_sql):
    from semantica_dataagent import discovery_executor

    with pytest.raises(discovery_executor.DiscoverySqlValidationError) as exc_info:
        discovery_executor.ensure_readonly_select_sql(bad_sql, require_limit=True)
    assert exc_info.value.status_code == 422


def test_execute_missing_token_returns_400_and_unconfigured_executor_returns_503(
    db_env,
):
    from semantica_dataagent import discovery_executor

    owner_id, _ = db_env
    sql = "SELECT 1 AS x FROM shop.orders LIMIT 10"

    # Missing personal DTB token -> 400
    with pytest.raises(discovery_executor.DiscoveryTokenMissingError) as exc_400:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=owner_id, sql=sql, limit=10
            )
        )
    assert exc_400.value.status_code == 400

    # Personal token present, executor not enabled -> 503
    _save_personal_token(owner_id, "personal-secret-token-123")
    with pytest.raises(discovery_executor.DiscoveryExecutorUnavailableError) as exc_503:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=owner_id, sql=sql, limit=10
            )
        )
    assert exc_503.value.status_code == 503


def test_fetch_query_history_success_and_query_audit_persistence(db_env, monkeypatch):
    from semantica_dataagent import discovery_executor, project_context
    from semantica_dataagent.db import QueryAudit, sessions
    from semantica_dataagent.dtb_executor import DtbExecutor

    owner_id, _ = db_env
    _save_personal_token(owner_id, "real-user-dtb-tok-999")
    _enable_dtb_executor()

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("token") == "real-user-dtb-tok-999"
        if request.url.path == "/api/data-query/async-execute":
            captured["sql"] = request.read().decode("utf-8")
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "qk-1"}}
            )
        if request.url.path == "/api/data-query/result/qk-1":
            return httpx.Response(
                200,
                json={
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
                        "data": [
                            [
                                "SELECT SUM(amount) FROM shop.orders WHERE dt='2026-11-18'",
                                "EOF",
                                "2026-11-18 10:00:00",
                                "shop",
                                "analyst_a",
                            ],
                            [
                                "SELECT 1",
                                "ERR",  # filtered out defensively
                                "2026-11-18 10:05:00",
                                "shop",
                                "analyst_b",
                            ],
                        ],
                    },
                },
            )
        raise AssertionError(f"Unexpected path {request.url.path}")

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        DtbExecutor,
        "bind",
        lambda self, params, tok: DtbExecutor(
            params["base_url"],
            tok,
            client=async_client,
            retry_delays=(),
            poll_interval=0,
        ),
    )

    cfg = project_context.discovery_config("default")
    fetched = asyncio.run(
        discovery_executor.fetch_query_history(owner_id=owner_id, config=cfg)
    )
    assert fetched.raw_row_count == 2
    assert len(fetched.records) == 1
    assert fetched.records[0].caller == "analyst_a"
    assert fetched.records[0].database == "shop"

    with sessions()() as db:
        audit = db.get(QueryAudit, fetched.audit_id)
        assert audit is not None
        assert audit.owner_id == owner_id
        assert audit.kind == "discovery_history"
        assert audit.status == "succeeded"
        assert audit.row_count == 2
        assert audit.elapsed_ms >= 0
        assert "LIMIT 5000" in audit.sql


def test_dtb_auth_401_aborts_immediately_and_scrubs_raw_errors(db_env, monkeypatch):
    from semantica_dataagent import discovery_executor
    from semantica_dataagent.db import QueryAudit, sessions
    from semantica_dataagent.dtb_executor import DtbExecutor

    owner_id, _ = db_env
    secret_tok = "satoken-super-secret-xyz"
    _save_personal_token(owner_id, secret_tok)
    _enable_dtb_executor()

    # 1. Real DTB HTTP 200 with body status "401" -> immediate 401 abort (single call, no retries)
    calls = {"count": 0}

    def auth_fail_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={"status": "401", "message": f"invalid satoken={secret_tok}"},
        )

    client1 = httpx.AsyncClient(transport=httpx.MockTransport(auth_fail_handler))
    monkeypatch.setattr(
        DtbExecutor,
        "bind",
        lambda self, params, tok: DtbExecutor(
            params["base_url"],
            tok,
            client=client1,
            retry_delays=(1, 2),
            poll_interval=0,
        ),
    )

    with pytest.raises(discovery_executor.DiscoveryAuthenticationError) as exc_401:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=owner_id,
                sql="SELECT 1 FROM shop.orders LIMIT 5",
                limit=5,
                kind="discovery_probe",
            )
        )
    assert exc_401.value.status_code == 401
    assert calls["count"] == 1
    assert secret_tok not in str(exc_401.value)

    # 2. Raw DTB poll errorMessage containing sensitive text must NEVER leak
    raw_secret_err = (
        f"Internal Doris error: password=db_pass_999 cookie=satoken={secret_tok}"
    )

    def raw_err_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/data-query/async-execute":
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "qk-err"}}
            )
        return httpx.Response(
            200,
            json={
                "status": "200",
                "data": {"progress": 1.0, "errorMessage": raw_secret_err},
            },
        )

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(raw_err_handler))
    monkeypatch.setattr(
        DtbExecutor,
        "bind",
        lambda self, params, tok: DtbExecutor(
            params["base_url"],
            tok,
            client=client2,
            retry_delays=(),
            poll_interval=0,
        ),
    )

    with pytest.raises(discovery_executor.DiscoveryExecutionError) as exc_502:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=owner_id,
                sql="SELECT 1 FROM shop.orders LIMIT 5",
                limit=5,
                kind="discovery_probe",
            )
        )
    assert exc_502.value.status_code == 502
    assert "db_pass_999" not in str(exc_502.value)
    assert secret_tok not in str(exc_502.value)
    assert "Internal Doris" not in str(exc_502.value)

    with sessions()() as db:
        audits = db.query(QueryAudit).filter(QueryAudit.owner_id == owner_id).all()
        assert len(audits) == 2
        assert all(a.status == "failed" and a.kind == "discovery_probe" for a in audits)


def test_execute_enforces_timeout_and_audits_failure(db_env, monkeypatch):
    from semantica_dataagent import discovery_executor
    from semantica_dataagent.db import QueryAudit, sessions
    from semantica_dataagent.dtb_executor import DtbExecutor

    owner_id, _ = db_env
    _save_personal_token(owner_id, "tok-timeout")
    _enable_dtb_executor(timeout_seconds=30)

    class _SlowExecutor:
        async def submit(self, sql, limit):
            return "slow-key"

        async def wait(self, key):
            await asyncio.sleep(5)
            return {"columns": ["a"], "data": [[1]]}

    monkeypatch.setattr(DtbExecutor, "bind", lambda self, params, tok: _SlowExecutor())

    with pytest.raises(discovery_executor.DiscoveryTimeoutError) as exc_timeout:
        asyncio.run(
            discovery_executor.execute_readonly_dtb_query(
                owner_id=owner_id,
                sql="SELECT 1 FROM shop.orders LIMIT 5",
                limit=5,
                timeout_seconds=0.05,
            )
        )
    assert exc_timeout.value.status_code == 504

    with sessions()() as db:
        audits = db.query(QueryAudit).filter(QueryAudit.owner_id == owner_id).all()
        assert len(audits) == 1
        assert audits[0].status == "failed"
