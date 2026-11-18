"""Dual-mode 门禁评测引擎 (evolution_gate) tests (#295)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent import (
    knowledge,
    project_context,
    sql_validation,
    trajectories,
)
from semantica_dataagent.agent_runner import DONE, AgentEvent, AgentTurnRequest
from semantica_dataagent.agent_types import ProviderConfig
from semantica_dataagent.db import (
    Build,
    GenerationPointer,
    KnowledgeGeneration,
    sessions,
)


def _setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    return TestClient(app), admin_token


class _StubRunner:
    """Offline-mockable AgentRunner returning SQL keyed by active vs candidate generation."""

    agent_id = "pi"

    def __init__(self, project_id: str, candidate_marker_sql: dict[str, str]):
        self.project_id = project_id
        self.candidate_marker_sql = candidate_marker_sql
        self.seen_prompts: list[str] = []

    def available(self) -> bool:
        return True

    async def run(self, request: AgentTurnRequest):
        self.seen_prompts.append(request.prompt)
        ctx = project_context.current_project(self.project_id)
        is_candidate = False
        with sessions()() as db:
            row = db.get(KnowledgeGeneration, ctx.generation)
            is_candidate = bool(row and row.is_candidate)
        key = ("cand" if is_candidate else "active", request.prompt)
        sql = self.candidate_marker_sql.get(key) or self.candidate_marker_sql.get(
            ("default", request.prompt),
            "SELECT amount FROM shop.orders",
        )
        yield AgentEvent(
            DONE,
            {
                "text": f"SQL result for {request.prompt}",
                "sql": [{"sql": sql, "dialect": "doris"}],
                "citations": [{"ref": "table:shop.orders"}],
            },
        )


def _start_judge_server(
    verdicts: list[dict], *, wire_format: str = "openai-completions"
):
    requests_seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
            requests_seen.append(raw)
            idx = len(requests_seen) - 1
            verdict = verdicts[idx] if idx < len(verdicts) else verdicts[-1]
            if verdict.get("http_status", 200) != 200:
                self.send_response(verdict["http_status"])
                self.end_headers()
                self.wfile.write(b"judge error")
                return
            payload_text = verdict.get("raw_text") or json.dumps(verdict)
            if wire_format == "anthropic-messages":
                body = {
                    "id": "msg_judge",
                    "type": "message",
                    "content": [{"type": "text", "text": payload_text}],
                }
            else:
                body = {
                    "id": "chat_judge",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": payload_text},
                        }
                    ],
                }
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, requests_seen


def _prepare_project_with_generations(
    c: TestClient, admin_token: str, monkeypatch: pytest.MonkeyPatch, name: str
):
    headers = {"Authorization": f"Bearer {admin_token}"}
    proj = c.post(
        "/api/v1/projects",
        headers=headers,
        json={"name": name, "type": "warehouse"},
    )
    assert proj.status_code == 201
    project_id = proj.json()["id"]

    active_gen = f"{project_id}_formal_v1"
    cand_gen = f"{project_id}_cand_v1"
    with sessions().begin() as db:
        db.add(
            Build(
                project_id=project_id,
                status="succeeded",
                started_at="2026-01-01",
                counts="{}",
            )
        )
        db.add(
            KnowledgeGeneration(
                name=active_gen,
                project_id=project_id,
                build_id=1,
                is_candidate=False,
                created_at="2026-11-18T00:00:00Z",
            )
        )
        db.add(
            KnowledgeGeneration(
                name=cand_gen,
                project_id=project_id,
                build_id=0,
                is_candidate=True,
                created_at="2026-11-18T00:01:00Z",
            )
        )
        ptr = db.query(GenerationPointer).filter_by(project_id=project_id).first()
        if ptr:
            ptr.generation = active_gen
        else:
            db.add(GenerationPointer(project_id=project_id, generation=active_gen))

    def fake_search(query, scope, top_k, category=None, project_id=None):
        ctx = project_context.current_project(project_id)
        if ctx.generation == cand_gen:
            return [{"ref": "table:shop.orders", "type": "table", "score": 0.9}]
        if "问题_target" in query or "目标问题" in query:
            return [{"ref": "table:shop.wrong_table", "type": "table", "score": 0.9}]
        return [{"ref": "table:shop.orders", "type": "table", "score": 0.9}]

    monkeypatch.setattr(knowledge, "search", fake_search)
    return project_id, active_gen, cand_gen


def test_confirmed_regression_mode_enforces_max_20_zero_regression_and_no_holdout_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import evolution_gate

    assert evolution_gate.DEFAULT_MAX_EVAL_CASES == 20
    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        project_id, _active_gen, cand_gen = _prepare_project_with_generations(
            c, admin_token, monkeypatch, "Gate Reg"
        )
        # Write 25 confirmed questions including a holdout with secret SQL
        q_rows = []
        for i in range(1, 26):
            is_holdout = i == 2
            q_text = "目标问题：统计支付GMV" if i == 1 else f"确认问题{i}"
            ref_sql = (
                "SELECT HOLDOUT_REF_SQL_SECRET FROM shop.orders"
                if is_holdout
                else "SELECT amount FROM shop.orders"
            )
            q_rows.append(
                {
                    "id": f"q-{i}",
                    "question": q_text,
                    "expected_tables": ["shop.orders"],
                    "expected_fields": ["shop.orders.amount"],
                    "caliber": "统计 orders 表 amount",
                    "reference_sql": ref_sql,
                    "dialect": "doris",
                    "split": "holdout" if is_holdout else "dev",
                    "answer_confirmed": True,
                }
            )
        q_path = project_context.eval_questions_path(project_id)
        q_path.parent.mkdir(parents=True, exist_ok=True)
        q_path.write_text(
            yaml.safe_dump({"questions": q_rows}, allow_unicode=True),
            encoding="utf-8",
        )
        before_yaml = q_path.read_text(encoding="utf-8")

        def fake_validate(sql, dialect="doris", project_id=None):
            if "bad_table" in sql:
                return {"status": "issues", "issues": [{"kind": "missing_table"}]}
            return {"status": "pass", "issues": []}

        monkeypatch.setattr(sql_validation, "validate_sql", fake_validate)

        # Scenario A: active fails target q-1; candidate fixes q-1 with zero regression
        runner_ok = _StubRunner(
            project_id,
            {
                ("active", "目标问题：统计支付GMV"): "SELECT x FROM shop.bad_table",
                ("cand", "目标问题：统计支付GMV"): "SELECT amount FROM shop.orders",
            },
        )
        report_ok = evolution_gate.evaluate_candidate_gate(
            project_id,
            candidate_generation=cand_gen,
            target_question_ids=["q-1"],
            runner=runner_ok,
        )
        assert report_ok["passed"] is True
        assert report_ok["mode"] == "confirmed_regression"
        assert report_ok["evaluated_cases"] == 20
        assert "HOLDOUT_REF_SQL_SECRET" not in json.dumps(report_ok, ensure_ascii=False)
        assert all("HOLDOUT_REF_SQL_SECRET" not in p for p in runner_ok.seen_prompts)
        assert q_path.read_text(encoding="utf-8") == before_yaml
        assert trajectories.list_trajectories(project_id) == []

        # Scenario B: candidate fixes q-1 but regresses confirmed question q-3 -> REJECT
        runner_reg = _StubRunner(
            project_id,
            {
                ("active", "目标问题：统计支付GMV"): "SELECT x FROM shop.bad_table",
                ("cand", "目标问题：统计支付GMV"): "SELECT amount FROM shop.orders",
                ("cand", "确认问题3"): "SELECT bad FROM shop.bad_table",
            },
        )
        report_reg = evolution_gate.evaluate_candidate_gate(
            project_id,
            candidate_generation=cand_gen,
            target_question_ids=["q-1"],
            runner=runner_reg,
        )
        assert report_reg["passed"] is False
        assert "q-3" in report_reg["regressions"]

        # Scenario C: zero improvement / tie (both retrieval & SQL tie) -> REJECT
        monkeypatch.setattr(
            knowledge,
            "search",
            lambda *a, **kw: [
                {"ref": "table:shop.orders", "type": "table", "score": 0.9}
            ],
        )
        runner_tie = _StubRunner(
            project_id,
            {
                ("active", "目标问题：统计支付GMV"): "SELECT amount FROM shop.orders",
                ("cand", "目标问题：统计支付GMV"): "SELECT amount FROM shop.orders",
            },
        )
        report_tie = evolution_gate.evaluate_candidate_gate(
            project_id,
            candidate_generation=cand_gen,
            target_question_ids=["q-1"],
            runner=runner_tie,
        )
        assert report_tie["passed"] is False


def test_cold_start_ab_judge_isolates_diagnosis_swaps_anonymously_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import evolution_gate

    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        project_id, active_gen, cand_gen = _prepare_project_with_generations(
            c, admin_token, monkeypatch, "Gate Cold"
        )
        # Only 2 confirmed questions (< 5) + 1 holdout question text
        q_path = project_context.eval_questions_path(project_id)
        q_path.parent.mkdir(parents=True, exist_ok=True)
        q_path.write_text(
            yaml.safe_dump(
                {
                    "questions": [
                        {
                            "id": "hc-1",
                            "question": "HOLDOUT_QUESTION_EXCLUDED",
                            "expected_tables": ["shop.orders"],
                            "expected_fields": ["shop.orders.amount"],
                            "caliber": "holdout",
                            "reference_sql": "SELECT amount FROM shop.orders",
                            "dialect": "doris",
                            "split": "holdout",
                            "answer_confirmed": True,
                        },
                        {
                            "id": "dc-1",
                            "question": "确认问题1",
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

        # Record 12 chronological trajectories:
        # - first 2 will be passed as diagnosis_trajectory_ids (must be excluded!)
        # - 1 matches holdout question text (must be excluded!)
        # - remaining 9 -> frozen last 30% = ceil(9 * 0.30) = 3 newest trajectories
        base_time = datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC)
        diag_ids: list[int] = []
        for idx in range(12):
            t = base_time + timedelta(minutes=idx * 5)
            conv_id = f"cold-conv-{idx}"
            q_text = (
                "HOLDOUT_QUESTION_EXCLUDED" if idx == 5 else f"冷启动轨迹问题_{idx}"
            )
            trajectories.start_online_trajectory(
                project_id=project_id,
                conversation_id=conv_id,
                turn_id=idx + 1,
                question=q_text,
                now=t,
            )
            trajectories.finish_online_trajectory(
                conversation_id=conv_id,
                turn_id=idx + 1,
                status="completed",
                answer="ok",
                final_sql=[
                    {"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}
                ],
                now=t,
            )
        all_traj = trajectories.list_trajectories(project_id)
        diag_ids = [all_traj[0]["id"], all_traj[1]["id"]]
        expected_reserved_questions = [
            all_traj[-3]["question"],
            all_traj[-2]["question"],
            all_traj[-1]["question"],
        ]

        monkeypatch.setattr(
            sql_validation,
            "validate_sql",
            lambda sql, dialect="doris", project_id=None: (
                {"status": "issues", "issues": [{"kind": "err"}]}
                if "bad_table" in sql
                else {"status": "pass", "issues": []}
            ),
        )

        runner = _StubRunner(
            project_id,
            {
                ("active", q): f"SELECT amount FROM shop.orders /* ACTIVE_TAG_{q} */"
                for q in expected_reserved_questions
            }
            | {
                ("cand", q): f"SELECT amount FROM shop.orders /* CAND_TAG_{q} */"
                for q in expected_reserved_questions
            },
        )

        # Judge picks whichever slot contains CAND_TAG for cases 0 and 1, and ACTIVE_TAG for case 2
        server, _judge_reqs = _start_judge_server([], wire_format="openai-completions")
        # Customize Judge handler dynamically to inspect A/B anonymization & swapped slots
        server.shutdown()

        observed_prompts: list[str] = []
        ab_positions_of_candidate: list[str] = []
        mode_switch = {"kind": "cand_wins_2_1"}

        class SmartJudgeHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                msg = body["messages"][-1]["content"]
                observed_prompts.append(msg)
                idx = len(observed_prompts) - 1
                a_part = msg.split("Option B:")[0]
                cand_slot = "A" if "CAND_TAG_" in a_part else "B"
                active_slot = "B" if cand_slot == "A" else "A"
                ab_positions_of_candidate.append(cand_slot)

                if mode_switch["kind"] == "cand_wins_2_1":
                    winner = cand_slot if idx in (0, 1) else active_slot
                    payload = {
                        "winner": winner,
                        "critical_error_a": False,
                        "critical_error_b": False,
                    }
                elif mode_switch["kind"] == "critical_error":
                    payload = {
                        "winner": cand_slot,
                        "critical_error_a": cand_slot == "A",
                        "critical_error_b": cand_slot == "B",
                    }
                elif mode_switch["kind"] == "tie":
                    payload = {
                        "winner": "tie",
                        "critical_error_a": False,
                        "critical_error_b": False,
                    }
                else:
                    self.send_response(500)
                    self.end_headers()
                    return

                out = json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(payload),
                                },
                            }
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, format, *args):
                return

        judge_srv = ThreadingHTTPServer(("127.0.0.1", 0), SmartJudgeHandler)
        threading.Thread(target=judge_srv.serve_forever, daemon=True).start()
        try:
            judge_cfg = ProviderConfig(
                id="judge-p",
                name="Judge",
                type="openai-completions",
                base_url=f"http://127.0.0.1:{judge_srv.server_address[1]}",
                models=["judge-model"],
                api_key="judge-key",
            )

            # 1. Valid win (W_c=2 > W_p=1, E_c=0) with swapped slots across runs/cases
            res_pass = evolution_gate.evaluate_candidate_gate(
                project_id,
                candidate_generation=cand_gen,
                diagnosis_trajectory_ids=diag_ids,
                runner=runner,
                judge_provider=judge_cfg,
                judge_model="judge-model",
                seed=7,
            )
            assert res_pass["passed"] is True
            assert res_pass["mode"] == "cold_start_ab"
            assert res_pass["evaluated_cases"] == 3
            assert res_pass["W_c"] == 2
            assert res_pass["W_p"] == 1
            assert res_pass["E_c"] == 0
            # Verify neither diagnosis trajectories nor holdout entered Judge
            joined = "\n".join(observed_prompts)
            assert "HOLDOUT_QUESTION_EXCLUDED" not in joined
            assert "Question: 冷启动轨迹问题_0\n" not in joined
            assert "Question: 冷启动轨迹问题_1\n" not in joined
            # Verify blank context & anonymized slots (both A and B positions exercised)
            assert len(set(ab_positions_of_candidate)) == 2
            assert active_gen not in joined and cand_gen not in joined

            # 2. Candidate introduces a new validate_sql error -> REJECT
            bad_runner = _StubRunner(
                project_id,
                {
                    (
                        "active",
                        expected_reserved_questions[0],
                    ): "SELECT amount FROM shop.orders",
                    (
                        "cand",
                        expected_reserved_questions[0],
                    ): "SELECT x FROM shop.bad_table",
                },
            )
            res_sql_err = evolution_gate.evaluate_candidate_gate(
                project_id,
                candidate_generation=cand_gen,
                diagnosis_trajectory_ids=diag_ids,
                runner=bad_runner,
                judge_provider=judge_cfg,
                judge_model="judge-model",
                seed=7,
            )
            assert res_sql_err["passed"] is False
            assert res_sql_err["new_validate_errors"] >= 1

            # 3. Judge returns critical_error on candidate -> REJECT even if winner=cand
            mode_switch["kind"] = "critical_error"
            res_crit = evolution_gate.evaluate_candidate_gate(
                project_id,
                candidate_generation=cand_gen,
                diagnosis_trajectory_ids=diag_ids,
                runner=runner,
                judge_provider=judge_cfg,
                judge_model="judge-model",
                seed=7,
            )
            assert res_crit["passed"] is False
            assert res_crit["E_c"] >= 1

            # 4. Judge ties -> strictly REJECT
            mode_switch["kind"] = "tie"
            res_tie = evolution_gate.evaluate_candidate_gate(
                project_id,
                candidate_generation=cand_gen,
                diagnosis_trajectory_ids=diag_ids,
                runner=runner,
                judge_provider=judge_cfg,
                judge_model="judge-model",
                seed=7,
            )
            assert res_tie["passed"] is False

            # 5. Judge HTTP failure -> fail closed REJECT
            mode_switch["kind"] = "http_error"
            res_err = evolution_gate.evaluate_candidate_gate(
                project_id,
                candidate_generation=cand_gen,
                diagnosis_trajectory_ids=diag_ids,
                runner=runner,
                judge_provider=judge_cfg,
                judge_model="judge-model",
                seed=7,
            )
            assert res_err["passed"] is False
        finally:
            judge_srv.shutdown()


def test_gate_builds_candidate_routes_mcp_eval_without_trajectory_pollution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from semantica_dataagent import evolution_gate
    from semantica_dataagent.db import generation_pointer

    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        headers = {"Authorization": f"Bearer {admin_token}"}
        proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Gate E2E", "type": "warehouse"},
        )
        assert proj.status_code == 201
        project_id = proj.json()["id"]

        # Pre-seed active generation + a single trajectory to trigger cold-start A/B
        active_gen = f"{project_id}_formal_base"
        with sessions().begin() as db:
            db.add(
                KnowledgeGeneration(
                    name=active_gen,
                    project_id=project_id,
                    build_id=1,
                    is_candidate=False,
                    created_at="2026-11-18T00:00:00Z",
                )
            )
            ptr = db.query(GenerationPointer).filter_by(project_id=project_id).first()
            if ptr:
                ptr.generation = active_gen
            else:
                db.add(GenerationPointer(project_id=project_id, generation=active_gen))

        trajectories.start_online_trajectory(
            project_id=project_id,
            conversation_id="cold-1",
            turn_id=1,
            question="查询退款金额",
        )
        trajectories.finish_online_trajectory(
            conversation_id="cold-1",
            turn_id=1,
            status="completed",
            answer="ok",
            final_sql=[{"sql": "SELECT 1", "dialect": "doris"}],
        )

        # Mock build_candidate_generation + search + validate_sql while keeping real /mcp/ auth & teardown
        seen_mcp_generations: list[str] = []

        def fake_build_cand(
            pid, *, overlay_files=None, candidate_source_dir=None, generation_name=None
        ):
            cand_gen = f"{pid}_cand_built999"
            cand_dir = project_context.candidate_generations_dir(pid) / cand_gen
            cand_dir.mkdir(parents=True, exist_ok=True)
            with sessions().begin() as db:
                db.add(
                    KnowledgeGeneration(
                        name=cand_gen,
                        project_id=pid,
                        build_id=0,
                        is_candidate=True,
                        created_at="2026-11-18T00:02:00Z",
                    )
                )
            return knowledge.CandidateGeneration(
                pid,
                cand_gen,
                cand_dir,
                knowledge.EmbeddingStats(),
                {"embedding_cache_hits": 1},
            )

        destroyed: list[str] = []

        def fake_destroy_cand(gen, pid=None):
            destroyed.append(gen)
            with sessions().begin() as db:
                row = db.get(KnowledgeGeneration, gen)
                if row is not None:
                    db.delete(row)

        monkeypatch.setattr(knowledge, "build_candidate_generation", fake_build_cand)
        monkeypatch.setattr(
            knowledge, "destroy_candidate_generation", fake_destroy_cand
        )

        def fake_search(query, scope, top_k, category=None, project_id=None):
            ctx = project_context.current_project(project_id)
            seen_mcp_generations.append(ctx.generation)
            return [
                {"ref": f"table:shop.{ctx.generation}", "type": "table", "score": 0.9}
            ]

        monkeypatch.setattr(knowledge, "search", fake_search)
        monkeypatch.setattr(
            sql_validation,
            "validate_sql",
            lambda sql, dialect="doris", project_id=None: {
                "status": "pass",
                "issues": [],
            },
        )

        class _McpCallingRunner:
            agent_id = "pi"

            def available(self):
                return True

            async def run(self, request: AgentTurnRequest):
                resp = await asyncio.to_thread(
                    c.post,
                    "/mcp/",
                    headers={
                        "Authorization": f"Bearer {request.mcp_token}",
                        "Accept": "application/json, text/event-stream",
                        "Host": "localhost:8000",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "search_knowledge",
                            "arguments": {"query": request.prompt, "scope": "tables"},
                        },
                    },
                )
                assert resp.status_code == 200, resp.text
                yield AgentEvent(
                    DONE,
                    {
                        "text": f"resp-{seen_mcp_generations[-1]}",
                        "sql": [{"sql": "SELECT 1", "dialect": "doris"}],
                        "citations": [],
                    },
                )

        report = evolution_gate.evaluate_candidate_gate(
            project_id,
            overlay_files={"knowledge/metrics/refund.md": "# 退款"},
            runner=_McpCallingRunner(),
            judge_fn=lambda prompt: {
                "winner": "A"
                if f"{project_id}_cand_built999" in prompt.split("Option B:")[0]
                else "B",
                "critical_error_a": False,
                "critical_error_b": False,
            },
            seed=1,
        )
        assert report["passed"] is True
        assert report["W_c"] == 1 and report["W_p"] == 0 and report["E_c"] == 0
        # Real /mcp/ requests hit both active_gen and candidate_gen dynamically
        assert active_gen in seen_mcp_generations
        assert f"{project_id}_cand_built999" in seen_mcp_generations
        # Candidate torn down deterministically & GenerationPointer untouched
        assert destroyed == [f"{project_id}_cand_built999"]
        with sessions()() as db:
            assert generation_pointer(db, project_id).generation == active_gen
        # Synthetic eval MCP calls did NOT pollute interaction_trajectories
        assert len(trajectories.list_trajectories(project_id)) == 1


def test_cold_start_ab_deduplicates_questions_and_excludes_diagnosis_question_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import evolution_gate

    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        project_id, _active_gen, cand_gen = _prepare_project_with_generations(
            c, admin_token, monkeypatch, "Gate Dedup"
        )
        base_time = datetime(2026, 11, 18, 9, 0, 0, tzinfo=UTC)
        # Trajectories:
        # 0: diag question "诊断错题 口径" (in diagnosis_trajectory_ids)
        # 1: repeat of diag question with different whitespace " 诊断错题\n口径 " (MUST also be excluded!)
        # 2: "重复问题A"
        # 3: "重复问题A" (duplicate)
        # 4: "唯一问题B"
        # 5: "唯一问题C"
        # 6: " 重复问题A " (latest chronological repeat -> deduplicated eligible = [B, C, A, D] -> recent 30% = ceil(4*0.3)=2 -> [A, D])
        # 7: "唯一问题D"
        questions_seq = [
            "诊断错题 口径",
            " 诊断错题\n口径 ",
            "重复问题A",
            "重复问题A",
            "唯一问题B",
            "唯一问题C",
            " 重复问题A ",
            "唯一问题D",
        ]
        for idx, q_text in enumerate(questions_seq):
            t = base_time + timedelta(minutes=idx * 5)
            conv = f"dedup-{idx}"
            trajectories.start_online_trajectory(
                project_id=project_id,
                conversation_id=conv,
                turn_id=idx + 1,
                question=q_text,
                now=t,
            )
            trajectories.finish_online_trajectory(
                conversation_id=conv,
                turn_id=idx + 1,
                status="completed",
                answer="ok",
                final_sql=[
                    {"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}
                ],
                now=t,
            )
        all_rows = trajectories.list_trajectories(project_id)
        diag_ids = [all_rows[0]["id"]]

        monkeypatch.setattr(
            sql_validation,
            "validate_sql",
            lambda sql, dialect="doris", project_id=None: {
                "status": "pass",
                "issues": [],
            },
        )
        runner = _StubRunner(
            project_id,
            {
                (
                    "cand",
                    " 重复问题A ",
                ): "SELECT amount FROM shop.orders /* CAND_TAG */",
                ("cand", "唯一问题D"): "SELECT amount FROM shop.orders /* CAND_TAG */",
            },
        )
        judged_prompts: list[str] = []

        def judge(prompt: str) -> dict:
            judged_prompts.append(prompt)
            a_part = prompt.split("Option B:")[0]
            winner = "A" if "CAND_TAG" in a_part else "B"
            return {
                "winner": winner,
                "critical_error_a": False,
                "critical_error_b": False,
            }

        report = evolution_gate.evaluate_candidate_gate(
            project_id,
            candidate_generation=cand_gen,
            diagnosis_trajectory_ids=diag_ids,
            runner=runner,
            judge_fn=judge,
            seed=3,
        )
        assert report["passed"] is True
        assert report["evaluated_cases"] == 2
        assert report["W_c"] == 2 and report["W_p"] == 0
        joined = "\n".join(judged_prompts)
        assert "诊断错题" not in joined
        assert "重复问题A" in joined
        assert "唯一问题D" in joined


def test_confirmed_regression_reserves_budget_for_explicit_target_questions_when_over_20(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import evolution_gate
    from semantica_dataagent.eval import Question

    c, admin_token = _setup_client(tmp_path, monkeypatch)
    with c:
        project_id, _active_gen, cand_gen = _prepare_project_with_generations(
            c, admin_token, monkeypatch, "Gate Budget"
        )
        q_rows = [
            {
                "id": f"q-{i}",
                "question": f"已确认回归题{i}",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["shop.orders.amount"],
                "caliber": "orders amount",
                "reference_sql": "SELECT amount FROM shop.orders",
                "dialect": "doris",
                "split": "dev",
                "answer_confirmed": True,
            }
            for i in range(1, 26)
        ]
        q_path = project_context.eval_questions_path(project_id)
        q_path.parent.mkdir(parents=True, exist_ok=True)
        q_path.write_text(
            yaml.safe_dump({"questions": q_rows}, allow_unicode=True),
            encoding="utf-8",
        )

        monkeypatch.setattr(
            knowledge,
            "search",
            lambda *a, **kw: [
                {"ref": "table:shop.orders", "type": "table", "score": 0.9}
            ],
        )
        monkeypatch.setattr(
            sql_validation,
            "validate_sql",
            lambda sql, dialect="doris", project_id=None: (
                {"status": "issues", "issues": [{"kind": "missing_table"}]}
                if "bad_table" in sql
                else {"status": "pass", "issues": []}
            ),
        )

        explicit_target = Question(
            id="target-extra-1",
            question="新增目标题：修复退款金额口径",
            expected_tables=["shop.orders"],
            expected_fields=["shop.orders.amount"],
            caliber="orders amount",
            reference_sql="SELECT amount FROM shop.orders",
            dialect="doris",
            answer_confirmed=False,
        )
        runner = _StubRunner(
            project_id,
            {
                ("active", explicit_target.question): "SELECT x FROM shop.bad_table",
                ("cand", explicit_target.question): "SELECT amount FROM shop.orders",
            },
        )
        report = evolution_gate.evaluate_candidate_gate(
            project_id,
            candidate_generation=cand_gen,
            target_questions=[explicit_target],
            runner=runner,
        )
        assert report["passed"] is True
        assert report["strict_improvement"] is True
        assert report["evaluated_cases"] == 20
        assert explicit_target.question in runner.seen_prompts
