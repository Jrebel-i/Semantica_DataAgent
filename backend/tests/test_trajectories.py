"""Tests for 问答轨迹 (InteractionTrajectory) persistence, scrubbing, windowing, and purge (#295)."""

from __future__ import annotations

import os
import signal
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet


def _alembic_config(db_path: Path) -> Config:
    from semantica_dataagent import cli

    config = Config()
    config.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _setup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    initialize()
    return db_path


def test_alembic_0039_single_head_and_upgrade_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "migrate.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())

    cfg = _alembic_config(db_path)
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert any(rev.revision == "0039" for rev in script.walk_revisions())

    command.upgrade(cfg, "0038")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO knowledge_generations (name, project_id, created_at) "
            "VALUES ('default_old', 'default', '2026-01-01')"
        )

    command.upgrade(cfg, "0039")
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "interaction_trajectories" in tables
        kg_cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(knowledge_generations)"
            ).fetchall()
        }
        assert "is_candidate" in kg_cols
        assert conn.execute(
            "SELECT is_candidate FROM knowledge_generations WHERE name='default_old'"
        ).fetchone() == (0,)

    command.downgrade(cfg, "0038")
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "interaction_trajectories" not in tables
        kg_cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(knowledge_generations)"
            ).fetchall()
        }
        assert "is_candidate" not in kg_cols


def test_trajectory_store_roundtrip_untruncated_and_strict_scrubbing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_db(tmp_path, monkeypatch)
    from semantica_dataagent import trajectories

    long_sql = (
        "SELECT "
        + ", ".join(f"col_{i}" for i in range(600))
        + " FROM shop.orders WHERE dt = '2026-01-01'"
    )
    assert len(long_sql) > 5000
    long_question = "请统计各渠道明细口径：" + ("说明片段" * 1500)
    assert len(long_question) > 5000

    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="conv-1",
        turn_id=101,
        token_id="tok-1",
        question=long_question
        + " Authorization: Bearer secret-hdr-val sk-live-secret999999999999",
        secrets=("my-runtime-secret",),
    )

    # Record untruncated search_knowledge, request_clarification, validate_sql, sample_table_data, execute_query
    trace_1 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-1",
        conversation_id="conv-1",
        turn_id=101,
        is_online_turn=True,
        tool_name="search_knowledge",
        arguments={"query": "GMV 口径 my-runtime-secret", "scope": "all", "top_k": 5},
        output={
            "result": [
                {"ref": "table:shop.orders", "type": "table", "score": 0.9},
                {"ref": "document:metrics/sales.md", "type": "document", "score": 0.8},
            ]
        },
        is_error=False,
        secrets=("my-runtime-secret",),
    )
    assert trace_1

    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-1",
        conversation_id="conv-1",
        turn_id=101,
        is_online_turn=True,
        tool_name="request_clarification",
        arguments={
            "questions": [
                {
                    "question": "口径确认？",
                    "options": [
                        {"label": "支付金额", "ref": "document:metrics/paid.md"},
                        {"label": "下单金额", "ref": "document:metrics/ordered.md"},
                    ],
                }
            ]
        },
        output={
            "clarification_id": "clar-1",
            "status": "pending",
            "questions": [
                {
                    "id": "q-1",
                    "question": "口径确认？",
                    "options": [
                        {
                            "id": "opt-1",
                            "label": "支付金额",
                            "ref": "document:metrics/paid.md",
                        },
                        {
                            "id": "opt-2",
                            "label": "下单金额",
                            "ref": "document:metrics/ordered.md",
                        },
                    ],
                }
            ],
        },
        is_error=False,
    )

    issues = [
        {
            "kind": "partition",
            "table": "shop.orders",
            "field": "dt",
            "message": "缺少分区条件",
            "suggestion": "WHERE dt = (SELECT MAX(dt) FROM shop.orders)",
        }
    ]
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-1",
        conversation_id="conv-1",
        turn_id=101,
        is_online_turn=True,
        tool_name="validate_sql",
        arguments={"sql": long_sql, "dialect": "doris"},
        output={"status": "issues", "issues": issues},
        is_error=False,
    )

    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-1",
        conversation_id="conv-1",
        turn_id=101,
        is_online_turn=True,
        tool_name="sample_table_data",
        arguments={"table": "shop.orders", "limit": 2},
        output={
            "table": "shop.orders",
            "sql": "SELECT id FROM shop.orders LIMIT 2",
            "columns": ["id", "customer_phone"],
            "rows": [["ord-1", "13800138000-secret-row"]],
            "row_count": 1,
            "errorMessage": "RAW_DTB_INTERNAL_ERROR_LEAK",
        },
        is_error=False,
    )

    trajectories.finish_online_trajectory(
        conversation_id="conv-1",
        turn_id=101,
        status="completed",
        answer="完整回答 password=hunter2",
        final_sql=[{"sql": long_sql, "dialect": "doris"}],
        citations=[{"ref": "table:shop.orders"}],
    )

    rows = trajectories.list_trajectories("default")
    assert len(rows) == 1
    row = rows[0]
    assert row["source_type"] == "online"
    assert row["conversation_id"] == "conv-1"
    assert row["turn_id"] == 101
    # Full untruncated question & SQL preserved
    assert long_question in row["question"]
    assert row["final_sql"] == [{"sql": long_sql, "dialect": "doris"}]
    assert row["sql_validations"][0]["sql"] == long_sql
    assert row["sql_validations"][0]["issues"] == issues
    assert row["clarifications"][0]["questions"][0]["options"][1]["id"] == "opt-2"
    assert set(row["matched_refs"]) >= {
        "table:shop.orders",
        "document:metrics/sales.md",
        "document:metrics/paid.md",
        "document:metrics/ordered.md",
    }

    # Strict scrubbing verification
    dumped = str(row)
    for forbidden in (
        "secret-hdr-val",
        "sk-live-secret999999999999",
        "my-runtime-secret",
        "13800138000-secret-row",
        "RAW_DTB_INTERNAL_ERROR_LEAK",
        "hunter2",
    ):
        assert forbidden not in dumped


def test_local_mcp_30_minute_idle_window_and_checkpoint_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_db(tmp_path, monkeypatch)
    from semantica_dataagent import trajectories

    t0 = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=15)
    t2 = t1 + timedelta(minutes=31)  # idle > 30 min -> new window

    trace_a1 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-local",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="search_knowledge",
        arguments={"query": "订单", "scope": "tables"},
        output={"result": [{"ref": "table:shop.orders"}]},
        is_error=False,
        now=t0,
    )
    trace_a2 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-local",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="validate_sql",
        arguments={"sql": "SELECT id FROM shop.orders", "dialect": "hive"},
        output={"status": "pass", "issues": []},
        is_error=False,
        now=t1,
    )
    trace_b1 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-local",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="search_knowledge",
        arguments={"query": "用户", "scope": "tables"},
        output={"result": [{"ref": "table:shop.users"}]},
        is_error=False,
        now=t2,
    )
    # Different project must stay isolated in its own window
    trace_other = trajectories.record_mcp_tool_call(
        project_id="otherproj",
        token_id="tok-local",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="search_knowledge",
        arguments={"query": "库存", "scope": "tables"},
        output={"result": [{"ref": "table:inv.stock"}]},
        is_error=False,
        now=t0,
    )

    assert trace_a1 == trace_a2
    assert trace_b1 != trace_a1
    assert trace_other != trace_a1

    default_rows = trajectories.list_trajectories("default")
    assert len(default_rows) == 2
    assert default_rows[0]["source_type"] == "local_mcp"
    assert len(default_rows[0]["tool_calls"]) == 2
    assert len(default_rows[1]["tool_calls"]) == 1

    # Checkpoint cursor query
    cursor_id = default_rows[0]["id"]
    after_cursor = trajectories.list_trajectories("default", after_id=cursor_id)
    assert [r["id"] for r in after_cursor] == [default_rows[1]["id"]]

    other_rows = trajectories.list_trajectories("otherproj")
    assert len(other_rows) == 1
    assert other_rows[0]["matched_refs"] == ["table:inv.stock"]


def test_trajectories_lock_is_fork_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_db(tmp_path, monkeypatch)
    from semantica_dataagent import trajectories

    held = threading.Event()
    release = threading.Event()

    def hold():
        with trajectories._write_lock:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(5)
    try:
        pid = os.fork()
        if pid == 0:
            try:
                trajectories.record_mcp_tool_call(
                    project_id="default",
                    token_id="fork-tok",
                    conversation_id=None,
                    turn_id=None,
                    is_online_turn=False,
                    tool_name="validate_sql",
                    arguments={"sql": "SELECT 1", "dialect": "hive"},
                    output={"status": "pass", "issues": []},
                    is_error=False,
                )
            except BaseException:  # noqa: BLE001
                os._exit(2)
            os._exit(0)
        deadline = time.monotonic() + 5
        while True:
            reaped, status = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                break
            if time.monotonic() >= deadline:
                os.kill(pid, signal.SIGABRT)
                os.waitpid(pid, 0)
                raise AssertionError("Forked child deadlocked on trajectory lock")
            time.sleep(0.02)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        release.set()
        thread.join(5)


def test_local_mcp_trace_id_never_collides_after_old_window_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_db(tmp_path, monkeypatch)
    from semantica_dataagent import trajectories
    from semantica_dataagent.db import sessions

    t0 = datetime(2026, 11, 18, 9, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=40)
    t2 = t1 + timedelta(minutes=40)
    t3 = t2 + timedelta(minutes=40)

    trace_1 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-exp",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="validate_sql",
        arguments={"sql": "SELECT 1", "dialect": "doris"},
        output={"status": "pass", "issues": []},
        is_error=False,
        now=t0,
    )
    trace_2 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-exp",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="validate_sql",
        arguments={"sql": "SELECT 2", "dialect": "doris"},
        output={"status": "pass", "issues": []},
        is_error=False,
        now=t1,
    )
    assert trace_2 != trace_1

    # Expire window 1 while window 2 stays active
    with sessions().begin() as db:
        deleted = trajectories.delete_expired_local_trajectories(
            db, t0 + timedelta(minutes=20)
        )
        assert deleted == 1

    trace_3 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-exp",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="validate_sql",
        arguments={"sql": "SELECT 3", "dialect": "doris"},
        output={"status": "pass", "issues": []},
        is_error=False,
        now=t2,
    )
    assert trace_3 != trace_2
    assert trace_3 != trace_1

    # Also expire all windows and create a fresh one -> still unique
    with sessions().begin() as db:
        trajectories.delete_expired_local_trajectories(db, t2 + timedelta(minutes=5))

    trace_4 = trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="tok-exp",
        conversation_id=None,
        turn_id=None,
        is_online_turn=False,
        tool_name="validate_sql",
        arguments={"sql": "SELECT 4", "dialect": "doris"},
        output={"status": "pass", "issues": []},
        is_error=False,
        now=t3,
    )
    assert len({trace_1, trace_2, trace_3, trace_4}) == 4
