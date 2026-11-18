"""Live Neo4j + Milvus integration tests for isolated 候选知识代 & gate (#295)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import yaml
from conftest import cleanup_test_generations
from cryptography.fernet import Fernet
from neo4j import GraphDatabase
from pymilvus import MilvusClient

from semantica_dataagent import (
    evolution_gate,
    knowledge,
    project_context,
    sql_validation,
)
from semantica_dataagent.agent_runner import DONE, AgentEvent, AgentTurnRequest
from semantica_dataagent.db import (
    Build,
    generation_pointer,
    sessions,
)

NEEDED = (
    "TEST_NEO4J_URI",
    "TEST_NEO4J_USER",
    "TEST_NEO4J_PASSWORD",
    "TEST_MILVUS_URI",
    "TEST_EMBEDDING_URL",
)
pytestmark = pytest.mark.skipif(
    not all(os.getenv(x) for x in NEEDED),
    reason="set TEST_NEO4J_URI, TEST_NEO4J_USER, TEST_NEO4J_PASSWORD, TEST_MILVUS_URI and TEST_EMBEDDING_URL for isolated test services",
)


def test_live_candidate_generation_gate_and_crash_orphan_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastapi.testclient import TestClient

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))

    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}
    formal_prefix = f"live295_{uuid.uuid4().hex[:8]}"

    with TestClient(app) as c:
        resp = c.put(
            "/api/v1/settings",
            headers=headers,
            json={
                "values": {
                    "neo4j_uri": os.environ["TEST_NEO4J_URI"],
                    "neo4j_user": os.environ["TEST_NEO4J_USER"],
                    "neo4j_password": os.environ["TEST_NEO4J_PASSWORD"],
                    "milvus_uri": os.environ["TEST_MILVUS_URI"],
                    "embedding_url": os.environ["TEST_EMBEDDING_URL"],
                    "embedding_model": os.environ.get(
                        "TEST_EMBEDDING_MODEL", "model/bge-m3"
                    ),
                }
            },
        )
        assert resp.status_code == 200, resp.text

        src_dir = tmp_path / "src"
        (src_dir / "knowledge" / "metrics").mkdir(parents=True, exist_ok=True)
        (src_dir / "knowledge" / "metrics" / "orders.md").write_text(
            "# 订单说明\n\n订单基础表。\n", encoding="utf-8"
        )
        assert (
            c.put(
                "/api/v1/settings",
                headers=headers,
                json={
                    "values": {
                        project_context.SOURCE_DIRECTORY_KEY: str(src_dir),
                        project_context.GENERATION_KEY: formal_prefix,
                    }
                },
            ).status_code
            == 200
        )

        # Build formal baseline generation
        with sessions().begin() as db:
            b = Build(
                project_id=project_context.DEFAULT_PROJECT_ID,
                build_type="full",
                status="running",
                started_at="2026-11-18T00:00:00Z",
                counts="{}",
            )
            db.add(b)
            db.flush()
            bid = b.id
        knowledge.run_build(bid)

        created_gens = [formal_prefix]
        try:
            with sessions()() as db:
                ptr = generation_pointer(db, project_context.DEFAULT_PROJECT_ID)
                assert ptr is not None
                created_gens.append(ptr.generation)

            # Evaluate candidate overlay through evolution_gate (confirming teardown & cache reuse)
            q_path = project_context.eval_questions_path(
                project_context.DEFAULT_PROJECT_ID
            )
            q_path.parent.mkdir(parents=True, exist_ok=True)
            questions = [
                {
                    "id": f"q-{i}",
                    "question": "退款金额如何定义_LIVE295"
                    if i == 1
                    else f"订单基础说明_{i}",
                    "expected_tables": ["shop.orders"],
                    "expected_fields": ["shop.orders.id"],
                    "caliber": "orders",
                    "reference_sql": "SELECT id FROM shop.orders",
                    "dialect": "doris",
                    "answer_confirmed": True,
                }
                for i in range(1, 6)
            ]
            q_path.write_text(
                yaml.safe_dump({"questions": questions}, allow_unicode=True),
                encoding="utf-8",
            )

            class _LiveStubRunner:
                agent_id = "pi"

                def available(self):
                    return True

                async def run(self, request: AgentTurnRequest):
                    yield AgentEvent(
                        DONE,
                        {
                            "text": "ok",
                            "sql": [
                                {
                                    "sql": "SELECT id FROM shop.orders",
                                    "dialect": "doris",
                                }
                            ],
                            "citations": [],
                        },
                    )

            monkeypatch.setattr(
                sql_validation,
                "validate_sql",
                lambda *a, **kw: {"status": "pass", "issues": []},
            )
            gate_report = evolution_gate.evaluate_candidate_gate(
                project_context.DEFAULT_PROJECT_ID,
                overlay_files={
                    "knowledge/metrics/refund.md": "# 退款口径\n\n退款金额如何定义_LIVE295。\n"
                },
                runner=_LiveStubRunner(),
            )
            cand_gen = gate_report["candidate_generation"]
            created_gens.append(cand_gen)

            # Verify candidate was torn down from live Neo4j & Milvus
            driver = GraphDatabase.driver(
                os.environ["TEST_NEO4J_URI"],
                auth=(os.environ["TEST_NEO4J_USER"], os.environ["TEST_NEO4J_PASSWORD"]),
            )
            try:
                with driver.session() as session:
                    cnt = session.run(
                        "MATCH (n:SDAEntity {generation:$gen}) RETURN count(n) AS c",
                        gen=cand_gen,
                    ).single()["c"]
                    assert cnt == 0
            finally:
                driver.close()
            mc = MilvusClient(uri=os.environ["TEST_MILVUS_URI"])
            try:
                assert not mc.has_collection(project_context.STORAGE_PREFIX + cand_gen)
            finally:
                mc.close()
        finally:
            cleanup_test_generations(created_gens)
