"""Tests for retired L2 candidate gate and ADR-0013 L1 evolution validation gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    return initialize()


def test_obsolete_l2_candidate_gate_is_retired_and_judge_payload_preserved():
    from semantica_dataagent import evolution_gate
    from semantica_dataagent.agent_types import ProviderConfig

    assert not hasattr(evolution_gate, "evaluate_candidate_gate")
    assert not hasattr(evolution_gate, "_evaluate_cold_start_ab")
    assert not hasattr(evolution_gate, "_evaluate_confirmed_regression")

    prov = ProviderConfig(
        id="p1",
        name="P1",
        type="openai-completions",
        base_url="http://127.0.0.1:18080/v1",
        models=["m1"],
    )
    payload = evolution_gate._judge_payload(prov, "m1", "test prompt")
    assert payload["model"] == "m1"
    assert payload["messages"] == [{"role": "user", "content": "test prompt"}]


def test_evolution_gate_validates_l1_and_anti_duplication_without_candidate_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import evolution_gate, knowledge
    from semantica_dataagent.db import (
        CorrectionProposal,
        GenerationPointer,
        KnowledgeGeneration,
        Project,
        ProjectSetting,
        sessions,
    )

    _setup_env(tmp_path, monkeypatch)
    src = tmp_path / "src_gate"
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
    (src / "overrides").mkdir()
    (src / "overrides" / "semantic_overrides.yaml").write_text(
        "version: 1\n", encoding="utf-8"
    )
    (src / "knowledge" / "metrics").mkdir(parents=True)
    (src / "knowledge" / "metrics" / "gmv.md").write_text(
        "# GMV 口径\n\n统计已支付订单金额。\n", encoding="utf-8"
    )

    with sessions().begin() as db:
        db.add(Project(id="gatep", name="Gate Proj", type="warehouse", status="active"))
        db.add(KnowledgeGeneration(name="gatep_g1", project_id="gatep", build_id=1))
        db.add(GenerationPointer(project_id="gatep", generation="gatep_g1"))
        db.add(
            ProjectSetting(project_id="gatep", key="source_directory", value=str(src))
        )
        db.add(
            CorrectionProposal(
                id="pending-g1",
                project_id="gatep",
                owner_id="u1",
                target="field:shop.orders.amount",
                patch="version: 1\n",
                original="",
                diff="diff",
                reason="已有金额字段说明",
                status="pending",
                source="evolution",
                target_kind="override",
                structured_patch=json.dumps(
                    {
                        "version": 1,
                        "fields": {
                            "shop.orders.amount": {
                                "database": "shop",
                                "table": "orders",
                                "name": "amount",
                                "instructions": "单位为分",
                            }
                        },
                    }
                ),
            )
        )

    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda summary=False, project_id=None: [
            {
                "database": "shop",
                "name": "orders",
                "fields": [
                    {"name": "amount", "type": "decimal"},
                    {"name": "status", "type": "string"},
                ],
            }
        ],
    )

    # 1. Valid override passes L1 & anti-duplication
    ok = evolution_gate.validate_evolution_candidate(
        "gatep",
        target="field:shop.orders.status",
        target_kind="override",
        resolved_path="overrides/semantic_overrides.yaml",
        patch="version: 1\n",
        structured_patch={
            "version": 1,
            "fields": {
                "shop.orders.status": {
                    "database": "shop",
                    "table": "orders",
                    "name": "status",
                    "synonyms": ["订单状态"],
                }
            },
        },
        reason="补充订单状态同义词",
    )
    assert ok["passed"] is True
    assert ok["is_duplicate"] is False

    # 2. Non-existent column fails L1
    bad = evolution_gate.validate_evolution_candidate(
        "gatep",
        target="field:shop.orders.missing_col",
        target_kind="override",
        resolved_path="overrides/semantic_overrides.yaml",
        patch="version: 1\n",
        structured_patch={
            "version": 1,
            "fields": {
                "shop.orders.missing_col": {
                    "database": "shop",
                    "table": "orders",
                    "name": "missing_col",
                    "comment": "不存在字段",
                }
            },
        },
        reason="bad field",
    )
    assert bad["passed"] is False

    # 3. Duplicate against pending proposal is flagged
    dup = evolution_gate.validate_evolution_candidate(
        "gatep",
        target="field:shop.orders.amount",
        target_kind="override",
        resolved_path="overrides/semantic_overrides.yaml",
        patch="version: 1\n",
        structured_patch={
            "version": 1,
            "fields": {
                "shop.orders.amount": {
                    "database": "shop",
                    "table": "orders",
                    "name": "amount",
                    "instructions": "单位为分",
                }
            },
        },
        reason="重复金额说明",
    )
    assert dup["is_duplicate"] is True
    assert dup["duplicate_of"] == "pending_proposal:pending-g1"


def test_standalone_eval_questions_endpoints_work_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent.app import app

    token = _setup_env(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app, headers=headers) as client:
        created = client.post(
            "/api/v1/eval-questions",
            json={
                "question": "昨日成交额是多少",
                "caliber": "统计已支付订单金额",
                "reference_sql": "SELECT SUM(amount) FROM shop.orders WHERE status = 'paid'",
                "dialect": "doris",
            },
        )
        assert created.status_code == 201, created.text
        listed = client.get("/api/v1/eval-questions")
        assert listed.status_code == 200
        assert len(listed.json()) == 1
