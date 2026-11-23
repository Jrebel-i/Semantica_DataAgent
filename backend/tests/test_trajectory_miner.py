"""Tests for trajectory_miner.py (#296): isolation, dedup/split, and 5 signal clusters."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))
    from semantica_dataagent.cli import initialize

    initialize()


def test_gap_review_time_and_successful_report_are_not_failures(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import trajectory_miner
    from semantica_dataagent.db import KnowledgeGap, sessions

    now = datetime.now(UTC)
    with sessions().begin() as db:
        for status in ("pending", "rejected", "confirmed"):
            db.add(
                KnowledgeGap(
                    id=status,
                    project_id="default",
                    owner_id="u",
                    question=status,
                    missing=status,
                    status=status,
                    created_at=now - timedelta(days=2),
                    reviewed_at=now if status != "pending" else None,
                )
            )
    signals = trajectory_miner._mine_gaps_and_failures(
        "default",
        [
            {
                "id": 1,
                "status": "success",
                "tool_calls": [{"tool_name": "report_knowledge_gap"}],
            }
        ],
        reserve_rows=[],
        holdout_conv_ids=set(),
        holdouts=set(),
        after_at=now - timedelta(days=1),
        now=now,
    )
    assert [s.details["kind"] for s in signals] == ["knowledge_gap"]
    assert signals[0].details["questions"] == ["confirmed"]


def test_miner_excludes_holdout_expired_deleted_and_splits_after_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import project_context, trajectories, trajectory_miner
    from semantica_dataagent.db import Conversation, KnowledgeGap, sessions

    # Write holdout + dev eval questions
    q_path = project_context.eval_questions_path("default")
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(
        yaml.safe_dump(
            {
                "questions": [
                    {
                        "id": "hold-1",
                        "question": "留出秘密指标口径是什么",
                        "expected_tables": ["shop.orders"],
                        "expected_fields": ["shop.orders.amount"],
                        "caliber": "HOLDOUT_CALIBER_SECRET",
                        "reference_sql": "SELECT HOLDOUT_SQL_SECRET FROM shop.orders",
                        "dialect": "doris",
                        "split": "holdout",
                        "answer_confirmed": True,
                    },
                    {
                        "id": "dev-1",
                        "question": "普通开发问题",
                        "expected_tables": ["shop.orders"],
                        "expected_fields": ["shop.orders.amount"],
                        "caliber": "ok",
                        "reference_sql": "SELECT amount FROM shop.orders",
                        "dialect": "doris",
                        "split": "dev",
                        "answer_confirmed": True,
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    base = datetime(2026, 11, 10, 8, 0, 0, tzinfo=UTC)

    # Create an expired (resumable=False) conversation whose trace must be excluded
    with sessions().begin() as db:
        db.add(
            Conversation(
                id="conv-expired",
                owner_id="u1",
                project_id="default",
                title="expired",
                agent="pi",
                provider_id="p1",
                model="m1",
                resumable=False,
                created_at=base,
                updated_at=base,
            )
        )
        db.add(
            KnowledgeGap(
                id="gap-holdout",
                project_id="default",
                owner_id="u1",
                question="留出秘密指标口径是什么",
                missing="should never leak",
                created_at=base + timedelta(minutes=1),
            )
        )

    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="conv-expired",
        turn_id=1,
        question="过期会话问题",
        now=base,
    )
    trajectories.finish_online_trajectory(
        conversation_id="conv-expired",
        turn_id=1,
        status="completed",
        answer="done",
        now=base,
    )

    # Holdout trajectory must be excluded even with trailing punctuation differences
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="conv-holdout",
        turn_id=2,
        question="留出秘密指标口径是什么？",
        now=base + timedelta(minutes=2),
    )
    trajectories.finish_online_trajectory(
        conversation_id="conv-holdout",
        turn_id=2,
        status="completed",
        answer="ok",
        now=base + timedelta(minutes=2),
    )

    # Duplicate & near-duplicate questions in same conversation and across conversations
    # Group 1 (earliest): conv-dup has 2 turns on same topic -> grouped into 1
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="conv-dup",
        turn_id=10,
        question="统计今日成交金额",
        now=base + timedelta(minutes=10),
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="conv-dup",
        turn_id=10,
        is_online_turn=True,
        tool_name="search_knowledge",
        arguments={"query": "成交", "scope": "all"},
        output={"result": []},
        is_error=False,
        now=base + timedelta(minutes=10),
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="conv-dup",
        turn_id=10,
        is_online_turn=True,
        tool_name="search_knowledge",
        arguments={"query": "orders amount", "scope": "all"},
        output={"result": [{"ref": "table:shop.orders"}]},
        is_error=False,
        now=base + timedelta(minutes=11),
    )
    trajectories.finish_online_trajectory(
        conversation_id="conv-dup",
        turn_id=10,
        status="completed",
        answer="ok",
        final_sql=[{"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}],
        now=base + timedelta(minutes=11),
    )
    # Near-duplicate in another conversation merges into Group 1
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="conv-dup-2",
        turn_id=11,
        question="统计今日成交金额？",
        now=base + timedelta(minutes=12),
    )
    trajectories.finish_online_trajectory(
        conversation_id="conv-dup-2",
        turn_id=11,
        status="completed",
        answer="ok",
        now=base + timedelta(minutes=12),
    )

    # 9 more distinct chronological conversations -> total 10 deduped groups -> 7 diag / 3 reserve
    for i in range(2, 11):
        ts = base + timedelta(minutes=20 + i)
        cid = f"conv-distinct-{i}"
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=cid,
            turn_id=100 + i,
            question=f"独立业务问题编号_{i}_RESERVE_TAG_{i}",
            now=ts,
        )
        trajectories.finish_online_trajectory(
            conversation_id=cid,
            turn_id=100 + i,
            status="completed",
            answer=f"RESERVE_ANSWER_{i}",
            final_sql=[{"sql": f"SELECT col_{i} FROM shop.t_{i}", "dialect": "doris"}],
            now=ts,
        )

    result = trajectory_miner.mine_project_trajectories("default")
    dumped = json.dumps(result.to_diagnosis_payload(), ensure_ascii=False)

    assert "HOLDOUT_SQL_SECRET" not in dumped
    assert "HOLDOUT_CALIBER_SECRET" not in dumped
    assert "留出秘密指标口径是什么" not in dumped
    assert "过期会话问题" not in dumped
    assert result.total_eligible_groups == 10
    assert len(result.diagnosis_groups) == 7
    assert len(result.reserve_groups) == 3
    # Reserve questions/SQLs must NEVER leak into diagnosis payload
    for idx in (8, 9, 10):
        assert f"RESERVE_TAG_{idx}" not in dumped
        assert f"RESERVE_ANSWER_{idx}" not in dumped
    assert set(result.diagnosis_trajectory_ids).isdisjoint(
        set(result.reserve_trajectory_ids)
    )


def test_miner_detects_all_five_signals_and_preserves_multi_caliber_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import trajectories, trajectory_miner
    from semantica_dataagent.db import KnowledgeGap, QueryExecution, sessions

    base = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)

    # 1) search_detour
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="sig-detour",
        turn_id=1,
        question="统计支付流水",
        now=base,
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="sig-detour",
        turn_id=1,
        is_online_turn=True,
        tool_name="search_knowledge",
        arguments={"query": "支付流水", "scope": "all"},
        output={"result": []},
        is_error=False,
        now=base,
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="sig-detour",
        turn_id=1,
        is_online_turn=True,
        tool_name="search_knowledge",
        arguments={"query": "dwd_payment", "scope": "tables"},
        output={"result": [{"ref": "table:shop.dwd_payment"}]},
        is_error=False,
        now=base + timedelta(seconds=10),
    )
    trajectories.finish_online_trajectory(
        conversation_id="sig-detour",
        turn_id=1,
        status="completed",
        answer="done",
        final_sql=[{"sql": "SELECT pay_amt FROM shop.dwd_payment", "dialect": "doris"}],
        now=base + timedelta(seconds=20),
    )

    # 2) validate_sql fail-then-pass self-repair (via read_task_sql)
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="sig-repair",
        turn_id=2,
        question="订单关联用户等级统计",
        now=base + timedelta(minutes=5),
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="sig-repair",
        turn_id=2,
        is_online_turn=True,
        tool_name="validate_sql",
        arguments={
            "sql": "SELECT o.id FROM shop.orders o JOIN shop.users u ON o.uid = u.id",
            "dialect": "doris",
        },
        output={
            "status": "issues",
            "issues": [{"kind": "unverified_join", "message": "缺少显式外键连接"}],
        },
        is_error=False,
        now=base + timedelta(minutes=5, seconds=5),
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="sig-repair",
        turn_id=2,
        is_online_turn=True,
        tool_name="read_task_sql",
        arguments={"task_id": "task:orders_join_users"},
        output={
            "sql": "SELECT o.id FROM shop.orders o JOIN shop.users u ON o.user_id = u.user_id"
        },
        is_error=False,
        now=base + timedelta(minutes=5, seconds=10),
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="sig-repair",
        turn_id=2,
        is_online_turn=True,
        tool_name="validate_sql",
        arguments={
            "sql": "SELECT o.id FROM shop.orders o JOIN shop.users u ON o.user_id = u.user_id",
            "dialect": "doris",
        },
        output={"status": "pass", "issues": []},
        is_error=False,
        now=base + timedelta(minutes=5, seconds=15),
    )
    trajectories.finish_online_trajectory(
        conversation_id="sig-repair",
        turn_id=2,
        status="completed",
        answer="repaired",
        final_sql=[
            {
                "sql": "SELECT o.id FROM shop.orders o JOIN shop.users u ON o.user_id = u.user_id",
                "dialect": "doris",
            }
        ],
        now=base + timedelta(minutes=5, seconds=20),
    )

    # 3) multi_turn_correction in same conversation
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="sig-multi",
        turn_id=3,
        question="统计活跃商品数",
        now=base + timedelta(minutes=10),
    )
    trajectories.finish_online_trajectory(
        conversation_id="sig-multi",
        turn_id=3,
        status="completed",
        answer="用了 shop.items",
        final_sql=[{"sql": "SELECT COUNT(*) FROM shop.items", "dialect": "doris"}],
        now=base + timedelta(minutes=10, seconds=10),
    )
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="sig-multi",
        turn_id=4,
        question="不对，活跃商品应该用 shop.active_items 并过滤 is_deleted = 0",
        now=base + timedelta(minutes=11),
    )
    trajectories.record_mcp_tool_call(
        project_id="default",
        token_id="u1",
        conversation_id="sig-multi",
        turn_id=4,
        is_online_turn=True,
        tool_name="validate_sql",
        arguments={
            "sql": "SELECT COUNT(*) FROM shop.active_items WHERE is_deleted = 0",
            "dialect": "doris",
        },
        output={"status": "pass", "issues": []},
        is_error=False,
        now=base + timedelta(minutes=11, seconds=5),
    )
    trajectories.finish_online_trajectory(
        conversation_id="sig-multi",
        turn_id=4,
        status="completed",
        answer="已修正为 shop.active_items",
        final_sql=[
            {
                "sql": "SELECT COUNT(*) FROM shop.active_items WHERE is_deleted = 0",
                "dialect": "doris",
            }
        ],
        now=base + timedelta(minutes=11, seconds=10),
    )

    # 4) divergent_clarification across two separate conversations
    clar_payload = {
        "questions": [
            {
                "id": "clar-gmv",
                "question": "请问“销售额”指哪种口径？",
                "options": [
                    {
                        "id": "opt-paid",
                        "label": "支付销售额（不含退款）",
                        "ref": "document:metrics/paid_gmv.md",
                    },
                    {
                        "id": "opt-ordered",
                        "label": "下单销售额（含未支付）",
                        "ref": "document:metrics/ordered_gmv.md",
                    },
                ],
            }
        ]
    }
    for idx, (cid, chosen_ref, chosen_label, sql_filter) in enumerate(
        [
            (
                "sig-clar-1",
                "document:metrics/paid_gmv.md",
                "支付销售额（不含退款）",
                "SELECT SUM(paid_amt) FROM shop.orders WHERE pay_status = 'paid'",
            ),
            (
                "sig-clar-2",
                "document:metrics/ordered_gmv.md",
                "下单销售额（含未支付）",
                "SELECT SUM(order_amt) FROM shop.orders",
            ),
        ]
    ):
        ts = base + timedelta(minutes=15 + idx)
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=cid,
            turn_id=10 + idx * 2,
            question=f"本月销售额是多少_{idx}",
            now=ts,
        )
        trajectories.record_mcp_tool_call(
            project_id="default",
            token_id="u1",
            conversation_id=cid,
            turn_id=10 + idx * 2,
            is_online_turn=True,
            tool_name="request_clarification",
            arguments=clar_payload,
            output=clar_payload,
            is_error=False,
            now=ts,
        )
        trajectories.finish_online_trajectory(
            conversation_id=cid,
            turn_id=10 + idx * 2,
            status="completed",
            answer="请确认口径",
            now=ts + timedelta(seconds=5),
        )
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=cid,
            turn_id=11 + idx * 2,
            question=f"选择：{chosen_label}",
            now=ts + timedelta(seconds=15),
        )
        trajectories.finish_online_trajectory(
            conversation_id=cid,
            turn_id=11 + idx * 2,
            status="completed",
            answer="已按所选口径生成",
            final_sql=[{"sql": sql_filter, "dialect": "doris"}],
            citations=[{"ref": chosen_ref}],
            now=ts + timedelta(seconds=20),
        )

    # 5) knowledge_gap + execution_failure
    with sessions().begin() as db:
        db.add(
            KnowledgeGap(
                id="gap-1",
                project_id="default",
                owner_id="u1",
                question="什么是核销率？",
                missing="缺少核销率定义",
                created_at=base + timedelta(minutes=20),
            )
        )
        db.add(
            QueryExecution(
                id="qe-fail-1",
                owner_id="u1",
                conversation_id="sig-fail",
                turn_id=30,
                plan_data=json.dumps(
                    {
                        "project_id": "default",
                        "execution_failure": {
                            "stage": "poll",
                            "category": "table_or_catalog",
                        },
                    }
                ),
                sql="SELECT * FROM missing_db.missing_tbl",
                status="failed",
                created_at=base + timedelta(minutes=21),
            )
        )

    # Add enough tail reserve conversations so all 5 signal conversations sit in the 70% diagnosis pool
    for j in range(4):
        ts = base + timedelta(minutes=30 + j)
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=f"tail-{j}",
            turn_id=50 + j,
            question=f"尾留出验证题_{j}",
            now=ts,
        )
        trajectories.finish_online_trajectory(
            conversation_id=f"tail-{j}",
            turn_id=50 + j,
            status="completed",
            answer="ok",
            now=ts,
        )

    res = trajectory_miner.mine_project_trajectories("default")
    kinds = {s.signal_kind for s in res.signals}
    assert {
        "search_detour",
        "sql_self_repair",
        "multi_turn_correction",
        "divergent_clarification",
        "knowledge_gap_or_failure",
    }.issubset(kinds)

    clar_signal = next(
        s for s in res.signals if s.signal_kind == "divergent_clarification"
    )
    assert clar_signal.preserve_multi_caliber is True
    assert len(clar_signal.caliber_variants) >= 2
    labels = {v["label"] for v in clar_signal.caliber_variants}
    assert "支付销售额（不含退款）" in labels
    assert "下单销售额（含未支付）" in labels

    repair_signal = next(s for s in res.signals if s.signal_kind == "sql_self_repair")
    assert repair_signal.details.get("join_pairs")


def test_miner_excludes_reserve_holdout_and_deleted_query_execution_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import eval_questions, trajectories, trajectory_miner
    from semantica_dataagent.db import (
        Conversation,
        QueryExecution,
        Setting,
        Turn,
        sessions,
    )

    eval_questions.upsert_eval_question(
        "default",
        question="保留题集 holdout 失败问题",
        caliber="holdout caliber",
        reference_sql="SELECT amount FROM shop.orders",
        expected_tables=["shop.orders"],
        expected_fields=["shop.orders.amount"],
        split="holdout",
        answer_confirmed=True,
    )

    base = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
    # Chronological 4 trajectories -> 3 diagnosis + 1 tail reserve (tail-conv-3)
    for idx in range(4):
        ts = base + timedelta(minutes=idx * 5)
        cid = f"split-conv-{idx}"
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=cid,
            turn_id=100 + idx,
            question=f"普通统计问题_{idx}",
            now=ts,
        )
        trajectories.finish_online_trajectory(
            conversation_id=cid,
            turn_id=100 + idx,
            status="completed",
            answer="ok",
            now=ts + timedelta(seconds=10),
        )

    # Also record a trajectory matching the holdout question
    trajectories.start_online_trajectory(
        project_id="default",
        conversation_id="holdout-conv",
        turn_id=200,
        question="保留题集 holdout 失败问题",
        now=base + timedelta(minutes=2),
    )
    trajectories.finish_online_trajectory(
        conversation_id="holdout-conv",
        turn_id=200,
        status="failed",
        answer="secret",
        now=base + timedelta(minutes=2, seconds=5),
    )

    with sessions().begin() as db:
        db.add(Setting(key="session_retention_days", value="7"))
        # Deleted / non-resumable conversation
        db.add(
            Conversation(
                id="deleted-conv",
                owner_id="u1",
                project_id="default",
                title="deleted",
                agent="pi",
                provider_id="p1",
                model="m1",
                resumable=False,
                updated_at=base + timedelta(minutes=1),
            )
        )
        # Retention-expired conversation
        db.add(
            Conversation(
                id="expired-conv",
                owner_id="u1",
                project_id="default",
                title="expired",
                agent="pi",
                provider_id="p1",
                model="m1",
                resumable=True,
                updated_at=base - timedelta(days=30),
            )
        )
        # Holdout turn without trajectory row
        db.add(
            Conversation(
                id="holdout-turn-conv",
                owner_id="u1",
                project_id="default",
                title="holdout",
                agent="pi",
                provider_id="p1",
                model="m1",
                resumable=True,
                updated_at=base + timedelta(minutes=1),
            )
        )
        db.add(
            Turn(
                id=301,
                conversation_id="holdout-turn-conv",
                sequence=1,
                question="保留题集 holdout 失败问题",
                status="failed",
                answer="",
                sql="",
                citations="[]",
            )
        )

        # 1) Failure in the reserve split conversation (split-conv-3) -> must be excluded
        db.add(
            QueryExecution(
                id="qe-reserve",
                owner_id="u1",
                conversation_id="split-conv-3",
                turn_id=103,
                plan_data=json.dumps(
                    {
                        "project_id": "default",
                        "execution_failure": {
                            "stage": "poll",
                            "category": "reserve_secret_failure",
                        },
                    }
                ),
                sql="SELECT * FROM secret_reserve.tbl",
                status="failed",
                created_at=base + timedelta(minutes=16),
            )
        )
        # 2) Failure in holdout conversation -> must be excluded
        db.add(
            QueryExecution(
                id="qe-holdout-conv",
                owner_id="u1",
                conversation_id="holdout-conv",
                turn_id=200,
                plan_data=json.dumps(
                    {
                        "project_id": "default",
                        "execution_failure": {
                            "stage": "poll",
                            "category": "holdout_conv_failure",
                        },
                    }
                ),
                sql="SELECT * FROM secret_holdout.tbl",
                status="failed",
                created_at=base + timedelta(minutes=3),
            )
        )
        # 3) Failure tied to holdout Turn -> must be excluded
        db.add(
            QueryExecution(
                id="qe-holdout-turn",
                owner_id="u1",
                conversation_id="holdout-turn-conv",
                turn_id=301,
                plan_data=json.dumps(
                    {
                        "project_id": "default",
                        "execution_failure": {
                            "stage": "poll",
                            "category": "holdout_turn_failure",
                        },
                    }
                ),
                sql="SELECT * FROM secret_holdout_turn.tbl",
                status="failed",
                created_at=base + timedelta(minutes=3),
            )
        )
        # 4) Failure in deleted (resumable=False) conversation -> must be excluded
        db.add(
            QueryExecution(
                id="qe-deleted",
                owner_id="u1",
                conversation_id="deleted-conv",
                turn_id=None,
                plan_data=json.dumps(
                    {
                        "project_id": "default",
                        "execution_failure": {
                            "stage": "poll",
                            "category": "deleted_conv_failure",
                        },
                    }
                ),
                sql="SELECT * FROM secret_deleted.tbl",
                status="failed",
                created_at=base + timedelta(minutes=3),
            )
        )
        # 5) Failure in retention-expired conversation -> must be excluded
        db.add(
            QueryExecution(
                id="qe-expired",
                owner_id="u1",
                conversation_id="expired-conv",
                turn_id=None,
                plan_data=json.dumps(
                    {
                        "project_id": "default",
                        "execution_failure": {
                            "stage": "poll",
                            "category": "expired_conv_failure",
                        },
                    }
                ),
                sql="SELECT * FROM secret_expired.tbl",
                status="failed",
                created_at=base + timedelta(minutes=3),
            )
        )

    res = trajectory_miner.mine_project_trajectories(
        "default", now=base + timedelta(hours=1)
    )
    assert res.reserve_trajectory_ids  # split-conv-3 is in reserve
    gap_or_fail_signals = [
        s for s in res.signals if s.signal_kind == "knowledge_gap_or_failure"
    ]
    assert gap_or_fail_signals == []
