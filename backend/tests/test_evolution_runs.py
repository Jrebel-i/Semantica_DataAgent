"""Tests for #296 evolution runs state machine, checkpoints, miner, scheduler, and API."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
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


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))
    from semantica_dataagent.cli import initialize

    initialize()
    return db_path


def test_alembic_0040_single_head_upgrade_downgrade_and_running_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db_path = tmp_path / "migrate.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())

    cfg = _alembic_config(db_path)
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert heads == ["0040"]

    command.upgrade(cfg, "0039")
    command.upgrade(cfg, "0040")

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "evolution_runs",
            "evolution_rounds",
            "evolution_checkpoints",
        }.issubset(tables)

        now = "2026-11-18T00:00:00+00:00"
        conn.execute(
            "INSERT INTO evolution_runs (id, project_id, trigger_type, status, started_at, created_at) "
            "VALUES ('run-1', 'default', 'manual', 'running', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO evolution_runs (id, project_id, trigger_type, status, started_at, created_at) "
            "VALUES ('run-other', 'alpha', 'manual', 'running', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO evolution_runs (id, project_id, trigger_type, status, started_at, created_at) "
            "VALUES ('run-done', 'default', 'manual', 'accepted', ?, ?)",
            (now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO evolution_runs (id, project_id, trigger_type, status, started_at, created_at) "
                "VALUES ('run-2', 'default', 'manual', 'running', ?, ?)",
                (now, now),
            )

    command.downgrade(cfg, "0039")
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert not (
            {"evolution_runs", "evolution_rounds", "evolution_checkpoints"} & tables
        )


def test_startup_recovery_marks_running_runs_incomplete_without_advancing_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import evolution_runner, knowledge
    from semantica_dataagent.db import (
        EvolutionCheckpoint,
        EvolutionRound,
        EvolutionRun,
        GenerationPointer,
        KnowledgeGeneration,
        sessions,
    )

    monkeypatch.setattr(knowledge, "delete_generation_data", lambda values: None)

    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
    with sessions().begin() as db:
        db.add(
            KnowledgeGeneration(
                name="default_active_v1",
                project_id="default",
                build_id=1,
                is_candidate=False,
                created_at=now.isoformat(),
            )
        )
        db.add(
            KnowledgeGeneration(
                name="default_cand_orphan",
                project_id="default",
                build_id=0,
                is_candidate=True,
                created_at=now.isoformat(),
            )
        )
        ptr = db.query(GenerationPointer).filter_by(project_id="default").first()
        if ptr is None:
            db.add(
                GenerationPointer(project_id="default", generation="default_active_v1")
            )
        else:
            ptr.generation = "default_active_v1"

        db.add(
            EvolutionCheckpoint(
                project_id="default",
                checkpoint_trajectory_id=42,
                checkpoint_at=now,
                last_run_id="prev-run",
                updated_at=now,
            )
        )
        db.add(
            EvolutionRun(
                id="interrupted-run",
                project_id="default",
                trigger_type="manual",
                status="running",
                checkpoint_before_trajectory_id=42,
                frozen_max_trajectory_id=88,
                started_at=now,
                created_at=now,
            )
        )
        db.add(
            EvolutionRound(
                id="interrupted-round",
                run_id="interrupted-run",
                project_id="default",
                round_number=1,
                signal_kind="search_detour",
                hypothesis="Add synonym for orders",
                decision="running",
                started_at=now,
            )
        )

    recovered = evolution_runner.recover_interrupted_evolution_runs()
    assert recovered == 1

    with sessions()() as db:
        run = db.get(EvolutionRun, "interrupted-run")
        assert run is not None
        assert run.status == "incomplete"
        assert run.stop_reason == "interrupted"
        assert run.checkpoint_after_trajectory_id is None
        assert run.finished_at is not None

        rnd = db.get(EvolutionRound, "interrupted-round")
        assert rnd is not None
        assert rnd.decision == "incomplete"
        assert rnd.finished_at is not None

        ckpt = db.get(EvolutionCheckpoint, "default")
        assert ckpt is not None
        assert ckpt.checkpoint_trajectory_id == 42
        assert ckpt.last_run_id == "prev-run"

        ptr = db.query(GenerationPointer).filter_by(project_id="default").first()
        assert ptr is not None
        assert ptr.generation == "default_active_v1"

        assert db.get(KnowledgeGeneration, "default_cand_orphan") is None


def _init_git_repo(root: Path, files: dict[str, str]) -> str:
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Init",
            "-c",
            "user.email=init@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=root,
        check=True,
    )
    return (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    )


def _start_http_llm_server(responses: list[dict]):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests_seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
            requests_seen.append(raw)
            idx = min(len(requests_seen) - 1, len(responses) - 1)
            item = responses[idx]
            content = (
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            )
            body = json.dumps(
                {
                    "id": f"resp-{idx}",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests_seen


def _install_mock_graph_and_candidate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    project_id: str,
    src_dir: Path,
    tables_meta: list[dict],
):
    from semantica_dataagent import knowledge, project_context
    from semantica_dataagent.db import GenerationPointer, KnowledgeGeneration, sessions

    active_gen = f"{project_id}_formal_v1"
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
        if ptr is None:
            db.add(GenerationPointer(project_id=project_id, generation=active_gen))
        else:
            ptr.generation = active_gen

    monkeypatch.setattr(
        knowledge, "describe", lambda summary=False, project_id="default": tables_meta
    )

    built_overlays: list[dict[str, str | None]] = []

    def fake_build_cand(
        pid, *, overlay_files=None, candidate_source_dir=None, generation_name=None
    ):
        cand_gen = f"{pid}_cand_{len(built_overlays) + 1}"
        cand_dir = project_context.candidate_generations_dir(pid) / cand_gen
        cand_dir.mkdir(parents=True, exist_ok=True)
        built_overlays.append(dict(overlay_files or {}))
        for rel, content in (overlay_files or {}).items():
            if content is not None:
                p = cand_dir / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
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
            pid, cand_gen, cand_dir, knowledge.EmbeddingStats(), {"candidate": 1}
        )

    def fake_destroy_cand(gen, pid=None):
        with sessions().begin() as db:
            row = db.get(KnowledgeGeneration, gen)
            if row is not None:
                db.delete(row)

    monkeypatch.setattr(knowledge, "build_candidate_generation", fake_build_cand)
    monkeypatch.setattr(knowledge, "destroy_candidate_generation", fake_destroy_cand)
    return active_gen, built_overlays


def test_evolver_rejects_round1_accepts_round2_and_respects_review_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json
    import subprocess
    from datetime import timedelta

    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import (
        evolution_runner,
        knowledge,
        project_context,
        sql_validation,
        trajectories,
    )
    from semantica_dataagent.agent_runner import DONE, AgentEvent, AgentTurnRequest
    from semantica_dataagent.agent_types import ProviderConfig
    from semantica_dataagent.db import (
        Build,
        CorrectionProposal,
        EvolutionCheckpoint,
        EvolutionRound,
        ProjectSetting,
        sessions,
    )

    src_dir = tmp_path / "source_default"
    init_sha = _init_git_repo(
        src_dir,
        {
            "tables.json": json.dumps(
                [{"database": "shop", "name": "orders", "comment": "订单表"}]
            ),
            "fields.json": json.dumps(
                [
                    {
                        "database": "shop",
                        "table": "orders",
                        "name": "amount",
                        "type": "decimal",
                        "comment": "金额",
                    }
                ]
            ),
        },
    )
    with sessions().begin() as db:
        from semantica_dataagent.db import Setting

        db.add(
            Setting(
                key=project_context.SOURCE_DIRECTORY_KEY,
                value=str(src_dir),
            )
        )

    tables_meta = [
        {
            "database": "shop",
            "name": "orders",
            "comment": "订单表",
            "fields": [{"name": "amount", "type": "decimal", "comment": "金额"}],
        }
    ]
    _active_gen, built_overlays = _install_mock_graph_and_candidate(
        monkeypatch, project_id="default", src_dir=src_dir, tables_meta=tables_meta
    )

    # Seed 4 chronological trajectories -> 3 diagnosis + 1 reserve
    base = datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC)
    for idx in range(4):
        ts = base + timedelta(minutes=idx * 10)
        cid = f"evo-conv-{idx}"
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=cid,
            turn_id=idx + 1,
            question=f"统计支付GMV_{idx}",
            now=ts,
        )
        if idx == 0:
            trajectories.record_mcp_tool_call(
                project_id="default",
                token_id="u1",
                conversation_id=cid,
                turn_id=idx + 1,
                is_online_turn=True,
                tool_name="search_knowledge",
                arguments={"query": "支付GMV", "scope": "all"},
                output={"result": []},
                is_error=False,
                now=ts,
            )
            trajectories.record_mcp_tool_call(
                project_id="default",
                token_id="u1",
                conversation_id=cid,
                turn_id=idx + 1,
                is_online_turn=True,
                tool_name="search_knowledge",
                arguments={"query": "shop.orders", "scope": "tables"},
                output={"result": [{"ref": "table:shop.orders"}]},
                is_error=False,
                now=ts + timedelta(seconds=5),
            )
        trajectories.finish_online_trajectory(
            conversation_id=cid,
            turn_id=idx + 1,
            status="completed",
            answer="ok",
            final_sql=[{"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}],
            now=ts + timedelta(seconds=10),
        )

    # Mock Evolver HTTP server: round 1 weak hypothesis (rejected by gate), round 2 good hypothesis (accepted)
    evolver_srv, evolver_reqs = _start_http_llm_server(
        [
            {
                "hypothesis": "ROUND1_WEAK_HYPOTHESIS",
                "signal_kind": "search_detour",
                "target": "table:shop.orders",
                "target_kind": "override",
                "reason": "补充弱同义词",
                "structured_patch": {
                    "version": 1,
                    "tables": {
                        "shop.orders": {
                            "database": "shop",
                            "name": "orders",
                            "synonyms": ["弱同义词_R1"],
                        }
                    },
                },
            },
            {
                "hypothesis": "ROUND2_STRONG_HYPOTHESIS",
                "signal_kind": "search_detour",
                "target": "table:shop.orders",
                "target_kind": "override",
                "reason": "为订单表补充支付GMV同义词",
                "structured_patch": {
                    "version": 1,
                    "tables": {
                        "shop.orders": {
                            "database": "shop",
                            "name": "orders",
                            "synonyms": ["支付GMV", "成交额"],
                        }
                    },
                },
            },
        ]
    )

    class _TwoRoundRunner:
        agent_id = "pi"

        def available(self):
            return True

        async def run(self, request: AgentTurnRequest):
            ctx = project_context.current_project("default")
            cand_dir = (
                project_context.candidate_generations_dir("default") / ctx.generation
            )
            override_file = cand_dir / "overrides" / "semantic_overrides.yaml"
            override_text = (
                override_file.read_text(encoding="utf-8")
                if override_file.is_file()
                else ""
            )
            if "支付GMV" in override_text:
                sql = "SELECT SUM(amount) FROM shop.orders /* GOOD_CAND */"
            elif "弱同义词_R1" in override_text:
                sql = "SELECT bad_col FROM shop.missing_tbl /* BAD_CAND_R1 */"
            else:
                sql = "SELECT amount FROM shop.orders /* ACTIVE */"
            yield AgentEvent(
                DONE,
                {
                    "text": sql,
                    "sql": [{"sql": sql, "dialect": "doris"}],
                    "citations": [{"ref": "table:shop.orders"}],
                },
            )

    monkeypatch.setattr(
        knowledge,
        "search",
        lambda *a, **kw: [{"ref": "table:shop.orders", "type": "table", "score": 0.9}],
    )
    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="doris", project_id=None: (
            {"status": "issues", "issues": [{"kind": "missing_table"}]}
            if "missing_tbl" in sql
            else {"status": "pass", "issues": []}
        ),
    )

    def fake_judge(prompt: str):
        a_part = prompt.split("Option B:")[0]
        if "GOOD_CAND" in prompt:
            winner = "A" if "GOOD_CAND" in a_part else "B"
        else:
            winner = "B" if "BAD_CAND_R1" in a_part else "A"
        return {
            "winner": winner,
            "critical_error_a": False,
            "critical_error_b": False,
            "reason": "test verdict",
        }

    try:
        evo_provider = ProviderConfig(
            id="evo-p",
            name="Evolver",
            type="openai-completions",
            base_url=f"http://127.0.0.1:{evolver_srv.server_address[1]}",
            models=["evo-model"],
        )
        # 1) With default evolution_require_review=True -> proposal stays pending, checkpoint advances
        run_res = evolution_runner.run_evolution(
            "default",
            trigger_type="manual",
            runner=_TwoRoundRunner(),
            evolution_provider=evo_provider,
            evolution_model="evo-model",
            judge_fn=fake_judge,
            seed=11,
        )
        assert run_res["status"] == "accepted"
        assert run_res["rounds_completed"] == 2
        assert run_res["proposal_id"]
        assert len(built_overlays) == 2

        # Verify Evolver LLM never received reserve question
        evo_prompt_dump = json.dumps(evolver_reqs, ensure_ascii=False)
        assert "统计支付GMV_3" not in evo_prompt_dump

        with sessions()() as db:
            rounds = (
                db.query(EvolutionRound)
                .filter(EvolutionRound.run_id == run_res["id"])
                .order_by(EvolutionRound.round_number.asc())
                .all()
            )
            assert [r.decision for r in rounds] == ["reject", "accept"]
            assert rounds[0].reject_reason

            prop = db.get(CorrectionProposal, run_res["proposal_id"])
            assert prop is not None
            assert prop.source == "evolution"
            assert prop.status == "pending"
            gate_data = json.loads(prop.gate_report or "{}")
            assert gate_data["l2_gate"]["passed"] is True
            assert gate_data["provenance"]["evolution_run_id"] == run_res["id"]

            ckpt = db.get(EvolutionCheckpoint, "default")
            assert ckpt is not None
            assert ckpt.checkpoint_trajectory_id == run_res["frozen_max_trajectory_id"]

        # Source git repo untouched while pending
        head_now = (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=src_dir)
            .decode()
            .strip()
        )
        assert head_now == init_sha

        # 2) Add new batch and set evolution_require_review=False -> auto_applied + Git commit + Build queued
        with sessions().begin() as db:
            db.add(
                ProjectSetting(
                    project_id="default",
                    key=project_context.EVOLUTION_REQUIRE_REVIEW_KEY,
                    value="false",
                )
            )
        for idx in range(4, 7):
            ts = base + timedelta(hours=1, minutes=idx * 5)
            cid = f"evo-conv-{idx}"
            trajectories.start_online_trajectory(
                project_id="default",
                conversation_id=cid,
                turn_id=idx + 1,
                question=f"统计退单额_{idx}",
                now=ts,
            )
            if idx == 4:
                trajectories.record_mcp_tool_call(
                    project_id="default",
                    token_id="u1",
                    conversation_id=cid,
                    turn_id=idx + 1,
                    is_online_turn=True,
                    tool_name="search_knowledge",
                    arguments={"query": "退单额", "scope": "all"},
                    output={"result": []},
                    is_error=False,
                    now=ts,
                )
                trajectories.record_mcp_tool_call(
                    project_id="default",
                    token_id="u1",
                    conversation_id=cid,
                    turn_id=idx + 1,
                    is_online_turn=True,
                    tool_name="search_knowledge",
                    arguments={"query": "shop.orders", "scope": "tables"},
                    output={"result": [{"ref": "table:shop.orders"}]},
                    is_error=False,
                    now=ts + timedelta(seconds=5),
                )
            trajectories.finish_online_trajectory(
                conversation_id=cid,
                turn_id=idx + 1,
                status="completed",
                answer="ok",
                final_sql=[
                    {"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}
                ],
                now=ts + timedelta(seconds=10),
            )

        run_auto = evolution_runner.run_evolution(
            "default",
            trigger_type="scheduler",
            runner=_TwoRoundRunner(),
            evolution_provider=evo_provider,
            evolution_model="evo-model",
            judge_fn=fake_judge,
            seed=11,
        )
        assert run_auto["status"] == "accepted"
        with sessions()() as db:
            prop2 = db.get(CorrectionProposal, run_auto["proposal_id"])
            assert prop2 is not None
            assert prop2.source == "evolution"
            assert prop2.status == "auto_applied"
            assert prop2.commit_sha is not None
            queued = (
                db.query(Build).filter(Build.correction_proposal_id == prop2.id).first()
            )
            assert queued is not None
            assert queued.build_type == "incremental"
        assert (src_dir / "overrides" / "semantic_overrides.yaml").is_file()
    finally:
        evolver_srv.shutdown()


def test_evolver_budget_exhausted_stays_incomplete_without_advancing_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json
    import subprocess
    from datetime import timedelta

    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import (
        evolution_runner,
        knowledge,
        project_context,
        sql_validation,
        trajectories,
    )
    from semantica_dataagent.agent_runner import DONE, AgentEvent, AgentTurnRequest
    from semantica_dataagent.db import (
        CorrectionProposal,
        EvolutionCheckpoint,
        EvolutionRound,
        Setting,
        sessions,
    )

    src_dir = tmp_path / "source_exhaust"
    init_sha = _init_git_repo(
        src_dir,
        {"tables.json": json.dumps([{"database": "shop", "name": "orders"}])},
    )
    with sessions().begin() as db:
        db.add(Setting(key=project_context.SOURCE_DIRECTORY_KEY, value=str(src_dir)))

    _active_gen, _ = _install_mock_graph_and_candidate(
        monkeypatch,
        project_id="default",
        src_dir=src_dir,
        tables_meta=[
            {
                "database": "shop",
                "name": "orders",
                "fields": [{"name": "amount", "type": "decimal"}],
            }
        ],
    )

    base = datetime(2026, 11, 18, 9, 0, 0, tzinfo=UTC)
    for idx in range(3):
        ts = base + timedelta(minutes=idx * 5)
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=f"ex-{idx}",
            turn_id=idx + 1,
            question=f"指标查询_{idx}",
            now=ts,
        )
        trajectories.record_mcp_tool_call(
            project_id="default",
            token_id="u1",
            conversation_id=f"ex-{idx}",
            turn_id=idx + 1,
            is_online_turn=True,
            tool_name="search_knowledge",
            arguments={"query": f"绕路词_{idx}", "scope": "all"},
            output={"result": []},
            is_error=False,
            now=ts,
        )
        trajectories.record_mcp_tool_call(
            project_id="default",
            token_id="u1",
            conversation_id=f"ex-{idx}",
            turn_id=idx + 1,
            is_online_turn=True,
            tool_name="search_knowledge",
            arguments={"query": "shop.orders", "scope": "tables"},
            output={"result": [{"ref": "table:shop.orders"}]},
            is_error=False,
            now=ts + timedelta(seconds=5),
        )
        trajectories.finish_online_trajectory(
            conversation_id=f"ex-{idx}",
            turn_id=idx + 1,
            status="completed",
            answer="ok",
            final_sql=[{"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}],
            now=ts + timedelta(seconds=10),
        )

    class _TieRunner:
        agent_id = "pi"

        def available(self):
            return True

        async def run(self, request: AgentTurnRequest):
            yield AgentEvent(
                DONE,
                {
                    "text": "SELECT amount FROM shop.orders",
                    "sql": [
                        {"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}
                    ],
                    "citations": [{"ref": "table:shop.orders"}],
                },
            )

    monkeypatch.setattr(
        knowledge,
        "search",
        lambda *a, **kw: [{"ref": "table:shop.orders", "type": "table", "score": 0.9}],
    )
    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="doris", project_id=None: {"status": "pass", "issues": []},
    )

    res = evolution_runner.run_evolution(
        "default",
        trigger_type="manual",
        max_rounds=3,
        runner=_TieRunner(),
        judge_fn=lambda prompt: {
            "winner": "tie",
            "critical_error_a": False,
            "critical_error_b": False,
            "reason": "tie",
        },
        seed=5,
    )
    assert res["status"] == "incomplete"
    assert res["stop_reason"] == "budget_exhausted"
    assert res["rounds_completed"] == 3
    assert res["proposal_id"] is None
    assert res["checkpoint_after_trajectory_id"] is None

    with sessions()() as db:
        rounds = (
            db.query(EvolutionRound)
            .filter(EvolutionRound.run_id == res["id"])
            .order_by(EvolutionRound.round_number.asc())
            .all()
        )
        assert len(rounds) == 3
        assert all(r.decision == "reject" for r in rounds)
        assert db.query(CorrectionProposal).count() == 0
        assert db.get(EvolutionCheckpoint, "default") is None

    head_now = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=src_dir)
        .decode()
        .strip()
    )
    assert head_now == init_sha


def test_evolver_preserves_divergent_clarification_variants_and_deletion_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import json
    from datetime import timedelta

    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import (
        evolution_runner,
        knowledge,
        project_context,
        project_deletion,
        sql_validation,
        trajectories,
    )
    from semantica_dataagent.agent_runner import DONE, AgentEvent, AgentTurnRequest
    from semantica_dataagent.db import (
        CorrectionProposal,
        EvolutionCheckpoint,
        EvolutionRound,
        EvolutionRun,
        Project,
        Setting,
        sessions,
    )

    src_dir = tmp_path / "source_clar"
    _init_git_repo(
        src_dir,
        {"tables.json": json.dumps([{"database": "shop", "name": "orders"}])},
    )
    with sessions().begin() as db:
        db.add(Setting(key=project_context.SOURCE_DIRECTORY_KEY, value=str(src_dir)))
        db.add(Project(id="keepproj", name="Keep Proj", type="warehouse"))
        db.add(
            EvolutionCheckpoint(
                project_id="keepproj",
                checkpoint_trajectory_id=7,
                checkpoint_at=datetime(2026, 11, 18, tzinfo=UTC),
            )
        )

    _install_mock_graph_and_candidate(
        monkeypatch,
        project_id="default",
        src_dir=src_dir,
        tables_meta=[
            {
                "database": "shop",
                "name": "orders",
                "fields": [{"name": "amount", "type": "decimal"}],
            }
        ],
    )

    base = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)
    clar_payload = {
        "questions": [
            {
                "id": "clar-gmv",
                "question": "销售额口径确认",
                "options": [
                    {
                        "id": "opt-paid",
                        "label": "支付销售额",
                        "ref": "document:metrics/paid.md",
                    },
                    {
                        "id": "opt-gross",
                        "label": "下单销售额",
                        "ref": "document:metrics/gross.md",
                    },
                ],
            }
        ]
    }
    for idx, (cid, ref, label, sql) in enumerate(
        [
            (
                "clar-a",
                "document:metrics/paid.md",
                "支付销售额",
                "SELECT SUM(paid_amt) FROM shop.orders",
            ),
            (
                "clar-b",
                "document:metrics/gross.md",
                "下单销售额",
                "SELECT SUM(gross_amt) FROM shop.orders",
            ),
        ]
    ):
        ts = base + timedelta(minutes=idx * 5)
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=cid,
            turn_id=10 + idx * 2,
            question=f"本月销售额_{idx}",
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
            answer="请确认",
            now=ts + timedelta(seconds=5),
        )
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=cid,
            turn_id=11 + idx * 2,
            question=f"选择 {label}",
            now=ts + timedelta(seconds=10),
        )
        trajectories.finish_online_trajectory(
            conversation_id=cid,
            turn_id=11 + idx * 2,
            status="completed",
            answer="ok",
            final_sql=[{"sql": sql, "dialect": "doris"}],
            citations=[{"ref": ref}],
            now=ts + timedelta(seconds=15),
        )

    # Add 2 reserve conversations so diagnosis gets both clar conversations
    for idx in range(2):
        ts = base + timedelta(minutes=20 + idx)
        trajectories.start_online_trajectory(
            project_id="default",
            conversation_id=f"clar-tail-{idx}",
            turn_id=100 + idx,
            question=f"留出验证_{idx}",
            now=ts,
        )
        trajectories.finish_online_trajectory(
            conversation_id=f"clar-tail-{idx}",
            turn_id=100 + idx,
            status="completed",
            answer="ok",
            final_sql=[{"sql": "SELECT amount FROM shop.orders", "dialect": "doris"}],
            now=ts,
        )

    class _PassRunner:
        agent_id = "pi"

        def available(self):
            return True

        async def run(self, request: AgentTurnRequest):
            ctx = project_context.current_project("default")
            cand_dir = (
                project_context.candidate_generations_dir("default") / ctx.generation
            )
            has_variants = (
                any(
                    "严禁" in p.read_text(encoding="utf-8")
                    for p in (cand_dir / "knowledge").glob("*.md")
                )
                if (cand_dir / "knowledge").is_dir()
                else False
            )
            tag = "/* MULTI_CALIBER */" if has_variants else "/* ACTIVE */"
            yield AgentEvent(
                DONE,
                {
                    "text": f"ok {tag}",
                    "sql": [
                        {
                            "sql": f"SELECT amount FROM shop.orders {tag}",
                            "dialect": "doris",
                        }
                    ],
                    "citations": [{"ref": "table:shop.orders"}],
                },
            )

    monkeypatch.setattr(
        knowledge,
        "search",
        lambda *a, **kw: [{"ref": "table:shop.orders", "type": "table", "score": 0.9}],
    )
    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="doris", project_id=None: {"status": "pass", "issues": []},
    )

    # Deliberately return a collapsed single-default candidate from evolver_fn -> replaced by multi-caliber guard!
    def bad_collapsing_evolver(**kwargs):
        return {
            "hypothesis": "把销售额拍扁成支付销售额",
            "signal_kind": "divergent_clarification",
            "target": "document:knowledge/collapsed.md",
            "target_kind": "document",
            "patch": "# 销售额\n默认指支付销售额。",
            "structured_patch": None,
            "reason": "单一口径默认",
        }

    def pick_multi_caliber_judge(prompt: str):
        a_part = prompt.split("Option B:")[0]
        winner = "A" if "MULTI_CALIBER" in a_part else "B"
        return {
            "winner": winner,
            "critical_error_a": False,
            "critical_error_b": False,
            "reason": "preserves variants",
        }

    res2 = evolution_runner.run_evolution(
        "default",
        trigger_type="manual",
        runner=_PassRunner(),
        evolver_fn=bad_collapsing_evolver,
        judge_fn=pick_multi_caliber_judge,
        seed=2,
    )
    assert res2["status"] == "accepted"
    with sessions()() as db:
        prop = db.get(CorrectionProposal, res2["proposal_id"])
        assert prop is not None
        assert "支付销售额" in prop.patch
        assert "下单销售额" in prop.patch
        assert "澄清" in prop.patch

    # Verify project_deletion._delete_database removes evolution rows for deleted project only
    with sessions().begin() as db:
        db.add(Project(id="delproj", name="Del Proj", type="warehouse"))
        db.add(
            EvolutionCheckpoint(
                project_id="delproj",
                checkpoint_trajectory_id=1,
                checkpoint_at=base,
            )
        )
        db.add(
            EvolutionRun(
                id="del-run-1",
                project_id="delproj",
                status="incomplete",
                started_at=base,
                created_at=base,
            )
        )
        db.add(
            EvolutionRound(
                id="del-rnd-1",
                run_id="del-run-1",
                project_id="delproj",
                round_number=1,
                signal_kind="search_detour",
                hypothesis="h",
                decision="reject",
                started_at=base,
            )
        )
    project_deletion._delete_database("delproj", {})
    with sessions()() as db:
        assert db.get(EvolutionCheckpoint, "delproj") is None
        assert db.get(EvolutionRun, "del-run-1") is None
        assert db.get(EvolutionRound, "del-rnd-1") is None
        assert db.get(EvolutionCheckpoint, "keepproj") is not None


def test_unset_evolution_models_fallback_to_default_online_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import evolution_runner
    from semantica_dataagent.db import Provider, Setting, sessions

    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
    with sessions().begin() as db:
        db.add(
            Provider(
                id="prov-default",
                name="DefaultOnline",
                type="openai-completions",
                base_url="https://api.example.com/v1",
                models='["gpt-default", "gpt-mini"]',
                created_at=now,
                updated_at=now,
            )
        )
        db.add(Setting(key="assistant_provider_id", value="prov-default"))
        db.add(Setting(key="assistant_model", value="gpt-default"))

    evo_prov, evo_mdl = evolution_runner._resolve_run_provider_pair(
        None, None, None, None
    )
    assert evo_prov is not None
    assert evo_prov.id == "prov-default"
    assert evo_mdl == "gpt-default"

    jdg_prov, jdg_mdl = evolution_runner._resolve_run_provider_pair(
        None, None, "prov-default", "gpt-mini"
    )
    assert jdg_prov is not None
    assert jdg_prov.id == "prov-default"
    assert jdg_mdl == "gpt-mini"


def test_build_overlay_files_prepares_real_candidate_workspace_for_doc_and_ossie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import evolution_runner, knowledge
    from semantica_dataagent.db import ProjectSetting, sessions

    src_dir = tmp_path / "source"
    _init_git_repo(src_dir, {"README.md": "base\n"})
    work_dir = tmp_path / "candidate-work"

    with sessions().begin() as db:
        db.add(
            ProjectSetting(
                project_id="default",
                key="source_directory",
                value=str(src_dir),
            )
        )

    # 1) Direct document path target
    doc_overlay = evolution_runner._build_overlay_files(
        "default",
        {
            "target_kind": "document",
            "target": "document:knowledge/clarifications.md",
            "patch": "# Clarifications\n- Variant A\n- Variant B\n",
            "structured_patch": None,
        },
    )
    assert all(isinstance(k, str) for k in doc_overlay)
    knowledge._prepare_candidate_workspace(src_dir, work_dir, doc_overlay)
    assert (work_dir / "knowledge/clarifications.md").read_text(encoding="utf-8") == (
        "# Clarifications\n- Variant A\n- Variant B\n"
    )

    # 2) Existing SDADocument ref mapped to its Neo4j source_file
    monkeypatch.setattr(knowledge, "read_graph", lambda values: None)
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda driver, cypher, params: [
            {"node": {"generated": False, "source_file": "knowledge/existing.md"}}
        ],
    )
    existing_overlay = evolution_runner._build_overlay_files(
        "default",
        {
            "target_kind": "document",
            "target": "document:doc-ref-1",
            "patch": "# Updated Existing\n",
            "structured_patch": None,
        },
    )
    assert list(existing_overlay) == ["knowledge/existing.md"]
    knowledge._prepare_candidate_workspace(src_dir, work_dir, existing_overlay)
    assert (work_dir / "knowledge/existing.md").read_text(encoding="utf-8") == (
        "# Updated Existing\n"
    )

    # 3) Ossie YAML file target
    ossie_overlay = evolution_runner._build_overlay_files(
        "default",
        {
            "target_kind": "ossie",
            "target": "ossie:ossie/orders.yaml",
            "patch": "ossie_version: 0.3.0.dev0\nname: default\n",
            "structured_patch": None,
        },
    )
    assert all(isinstance(k, str) for k in ossie_overlay)
    assert list(ossie_overlay) == ["ossie/orders.yaml"]
    knowledge._prepare_candidate_workspace(src_dir, work_dir, ossie_overlay)
    assert (work_dir / "ossie/orders.yaml").read_text(encoding="utf-8") == (
        "ossie_version: 0.3.0.dev0\nname: default\n"
    )
