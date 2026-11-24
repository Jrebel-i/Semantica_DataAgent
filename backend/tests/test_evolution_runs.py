"""Tests for TraceSource-based evolution_runner pipeline, watermark, isolation, and recovery."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_DTB_CONFIG_DIR", str(tmp_path / "dtb"))
    from semantica_dataagent.cli import initialize

    initialize()


def _init_git_repo(root: Path, files: dict[str, str]) -> str:
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


def _seed_project(
    project_id: str,
    *,
    source_dir: Path,
    project_type: str = "warehouse",
) -> None:
    from semantica_dataagent.db import (
        GenerationPointer,
        KnowledgeGeneration,
        Project,
        ProjectSetting,
        sessions,
    )

    with sessions().begin() as db:
        if db.get(Project, project_id) is None:
            db.add(
                Project(
                    id=project_id,
                    name=f"Proj {project_id}",
                    type=project_type,
                    status="active",
                )
            )
        gen = f"{project_id}_v1"
        if db.get(KnowledgeGeneration, gen) is None:
            db.add(KnowledgeGeneration(name=gen, project_id=project_id, build_id=1))
        ptr = db.query(GenerationPointer).filter_by(project_id=project_id).first()
        if ptr is None:
            db.add(GenerationPointer(project_id=project_id, generation=gen))
        else:
            ptr.generation = gen
        row = db.get(ProjectSetting, (project_id, "source_directory"))
        if row is None:
            db.add(
                ProjectSetting(
                    project_id=project_id,
                    key="source_directory",
                    value=str(source_dir),
                )
            )
        else:
            row.value = str(source_dir)


def _mock_graph(monkeypatch: pytest.MonkeyPatch, docs: list[dict], tables: list[dict]):
    from semantica_dataagent import knowledge

    def fake_execute(_store, query, params):
        if "SDADocument" in query and "coalesce(d.generated" in query:
            return docs
        if "SDAEntity" in query and params.get("ref"):
            for d in docs:
                if d["ref"] == params["ref"]:
                    return [{"node": dict(d)}]
        return []

    monkeypatch.setattr(knowledge, "read_graph", lambda _v: object())
    monkeypatch.setattr(knowledge, "execute", fake_execute)
    monkeypatch.setattr(
        knowledge, "describe", lambda summary=False, project_id=None: tables
    )
    monkeypatch.setattr(knowledge, "search", lambda *a, **kw: [])


def test_startup_recovery_marks_running_runs_incomplete_without_advancing_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import evolution_runner
    from semantica_dataagent.db import EvolutionRun, ProjectEvolutionConfig, sessions

    wm = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    with sessions().begin() as db:
        db.add(
            ProjectEvolutionConfig(
                project_id="default",
                enabled=True,
                trace_source_plugin="langfuse",
                last_trace_timestamp=wm,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            EvolutionRun(
                id="interrupted-run",
                project_id="default",
                trigger_type="manual",
                status="running",
                started_at=now,
                created_at=now,
            )
        )

    assert evolution_runner.recover_interrupted_evolution_runs() == 1
    assert evolution_runner.recover_interrupted_evolution_runs() == 0

    with sessions()() as db:
        run = db.get(EvolutionRun, "interrupted-run")
        assert run is not None
        assert run.status == "incomplete"
        assert run.stop_reason == "interrupted"
        assert run.finished_at is not None

        cfg = db.get(ProjectEvolutionConfig, "default")
        assert cfg is not None
        assert cfg.last_trace_timestamp.replace(tzinfo=UTC) == wm


def test_missing_trace_source_fails_with_actionable_error_without_watermark_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import evolution_runner

    res = evolution_runner.run_evolution("default", trigger_type="manual")
    assert res["status"] == "failed"
    assert res["stop_reason"] == "config_error"
    assert "未配置对话追踪源" in (res["error"] or "")


def test_zero_trace_noop_advances_watermark_and_global_langfuse_fallback_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import crypto, evolution_runner, plugins
    from semantica_dataagent.db import ProjectEvolutionConfig, Setting, sessions

    seen_queries = []

    class _FakeLangfuse:
        id = "langfuse"
        version = "1.0"
        display_name = "Langfuse"
        parameters_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "public_key": {"type": "string"},
                "secret_key": {"type": "string", "format": "password"},
            },
            "required": ["base_url", "public_key", "secret_key"],
        }

        def fetch_traces(self, query):
            seen_queries.append(query)
            return []

    base = dict(plugins.discover())
    base[("trace_source", "langfuse")] = _FakeLangfuse()
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))

    with sessions().begin() as db:
        db.add_all(
            [
                Setting(key="langfuse_enabled", value="true"),
                Setting(key="langfuse_url", value="https://lf.global.example"),
                Setting(key="langfuse_public_key", value="pk-global"),
                Setting(
                    key="langfuse_secret_key", value=crypto.encrypt("sk-global-secret")
                ),
            ]
        )

    t_now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    res = evolution_runner.run_evolution("default", trigger_type="manual", now=t_now)
    assert res["status"] == "succeeded", (
        f"error={res.get('error')} summary={res.get('summary')}"
    )
    assert res["stop_reason"] == "no_traces"
    assert res["traces_fetched"] == 0
    assert res["proposals_created"] == 0
    assert res["trace_source_plugin"] == "langfuse"
    assert len(seen_queries) == 1
    assert seen_queries[0].parameters["base_url"] == "https://lf.global.example"
    assert seen_queries[0].secrets["secret_key"] == "sk-global-secret"

    with sessions()() as db:
        cfg = db.get(ProjectEvolutionConfig, "default")
        assert cfg is not None
        assert cfg.last_trace_timestamp.replace(tzinfo=UTC) == t_now


def test_trace_evolution_synthesizes_pending_proposals_dedups_isolates_and_advances_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import (
        crypto,
        evolution_runner,
        knowledge_synthesizer,
        plugins,
    )
    from semantica_dataagent.db import (
        CorrectionProposal,
        ProjectEvolutionConfig,
        sessions,
    )
    from semantica_dataagent.plugins import TraceConversation, TraceMessage

    src = tmp_path / "src_evo"
    init_sha = _init_git_repo(
        src,
        {
            "overrides/semantic_overrides.yaml": "version: 1\n",
            "knowledge/metrics/gmv.md": "# GMV 口径\n\n统计已支付订单金额。\n",
        },
    )
    _seed_project("projalpha", source_dir=src, project_type="warehouse")

    docs = [
        {
            "ref": "document:metrics/gmv.md",
            "category": "metrics",
            "path": "gmv.md",
            "source_file": "knowledge/metrics/gmv.md",
            "title": "GMV 口径",
            "content": "# GMV 口径\n\n统计已支付订单金额。\n",
        }
    ]
    tables = [
        {
            "database": "shop",
            "name": "orders",
            "comment": "订单表",
            "fields": [
                {"name": "status", "type": "string", "comment": "状态"},
                {"name": "amount", "type": "decimal", "comment": "金额"},
            ],
        }
    ]
    _mock_graph(monkeypatch, docs, tables)

    secret_val = "sk-proj-alpha-secret-xyz"
    wm0 = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
    run_time = datetime(2026, 11, 24, 5, 0, tzinfo=UTC)

    with sessions().begin() as db:
        db.add(
            ProjectEvolutionConfig(
                project_id="projalpha",
                enabled=True,
                trace_source_plugin="fake-ts",
                trace_source_parameters=json.dumps({"endpoint": "https://ts.local"}),
                trace_source_encrypted_secrets=json.dumps(
                    {"api_key": crypto.encrypt(secret_val)}
                ),
                last_trace_timestamp=wm0,
                created_at=wm0,
                updated_at=wm0,
            )
        )

    recorded_queries = []

    class _FakeTraceSource:
        id = "fake-ts"
        version = "1.0"
        display_name = "Fake TraceSource"
        parameters_schema: ClassVar[dict] = {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "api_key": {"type": "string", "format": "password"},
            },
            "required": ["endpoint", "api_key"],
        }

        def fetch_traces(self, query):
            recorded_queries.append(query)
            return [
                # Wrong project -> MUST be dropped by defense-in-depth filter
                TraceConversation(
                    trace_id="tr-other-proj",
                    project_id="other_proj",
                    started_at=wm0 + timedelta(minutes=10),
                    messages=[TraceMessage(role="user", content="OTHER_PROJ_SECRET")],
                ),
                # Target project trace 1
                TraceConversation(
                    trace_id="tr-a1",
                    project_id="projalpha",
                    session_id="sess-1",
                    started_at=wm0 + timedelta(minutes=20),
                    ended_at=wm0 + timedelta(minutes=21),
                    messages=[
                        TraceMessage(
                            role="user",
                            content=f"GMV口径要剔除测试单，请勿泄露 {secret_val}",
                        ),
                        TraceMessage(
                            role="assistant",
                            content="好的，shop.orders.status 需要排除 test。",
                        ),
                    ],
                ),
                # Target project trace 2
                TraceConversation(
                    trace_id="tr-a2",
                    project_id="projalpha",
                    session_id="sess-2",
                    started_at=wm0 + timedelta(minutes=30),
                    ended_at=wm0 + timedelta(minutes=31),
                    messages=[
                        TraceMessage(
                            role="user", content="需要新建跨表退款净额规则文档"
                        ),
                    ],
                ),
            ]

    base = dict(plugins.discover())
    base[("trace_source", "fake-ts")] = _FakeTraceSource()
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))

    llm_contexts: list[dict] = []

    def fake_llm(ctx: dict) -> dict:
        llm_contexts.append(ctx)
        assert ctx["source_type"] == "evolution"
        assert ctx["project_id"] == "projalpha"
        assert "OTHER_PROJ_SECRET" not in json.dumps(ctx, ensure_ascii=False)
        assert secret_val not in json.dumps(ctx, ensure_ascii=False)
        return {
            "proposals": [
                # 1. Valid override
                {
                    "route": "override",
                    "reason": "补充订单状态字段使用约束，剔除测试状态",
                    "structured_patch": {
                        "version": 1,
                        "fields": {
                            "shop.orders.status": {
                                "database": "shop",
                                "table": "orders",
                                "name": "status",
                                "instructions": "统计 GMV 时剔除 test 测试单",
                            }
                        },
                    },
                },
                # 2. Valid merge into existing MD
                {
                    "route": "existing_document",
                    "target": "document:metrics/gmv.md",
                    "reason": "在 GMV 文档补充测试单剔除说明",
                    "patch": "# GMV 口径\n\n统计已支付订单金额。\n\n## 测试单剔除\n\n剔除 status=test。\n",
                },
                # 3. Valid new MD
                {
                    "route": "new_document",
                    "new_doc_path": "knowledge/rules/refund_net.md",
                    "reason": "新建跨表退款净额计算规则文档",
                    "patch": "# 退款净额规则\n\nGMV 减去退款成功金额。\n",
                },
                # 4. Duplicate of #1 inside same batch -> skipped!
                {
                    "route": "override",
                    "reason": "重复订单状态说明",
                    "structured_patch": {
                        "version": 1,
                        "fields": {
                            "shop.orders.status": {
                                "database": "shop",
                                "table": "orders",
                                "name": "status",
                                "instructions": "统计 GMV 时剔除 test 测试单",
                            }
                        },
                    },
                },
                # 5. Nonexistent column -> rejected by L1 static gate!
                {
                    "route": "override",
                    "reason": "无效字段",
                    "structured_patch": {
                        "version": 1,
                        "fields": {
                            "shop.orders.no_such_col": {
                                "database": "shop",
                                "table": "orders",
                                "name": "no_such_col",
                                "comment": "bad",
                            }
                        },
                    },
                },
            ]
        }

    # First run: incremental from wm0 -> run_time
    res = evolution_runner.run_evolution(
        "projalpha",
        trigger_type="scheduler",
        llm_fn=fake_llm,
        now=run_time,
    )
    assert res["status"] == "succeeded", (
        f"error={res.get('error')} summary={res.get('summary')}"
    )
    assert res["traces_fetched"] == 2
    assert res["proposals_created"] == 3
    assert res["duplicates_skipped"] == 1
    assert res["l1_rejected"] == 1
    assert len(res["proposal_ids"]) == 3
    assert res["window_since"] == wm0
    assert res["window_until"] == run_time

    assert len(recorded_queries) == 1
    assert recorded_queries[0].project_id == "projalpha"
    assert recorded_queries[0].secrets == {"api_key": secret_val}
    assert recorded_queries[0].since == wm0
    assert recorded_queries[0].until == run_time

    with sessions()() as db:
        cfg = db.get(ProjectEvolutionConfig, "projalpha")
        assert cfg is not None
        assert cfg.last_trace_timestamp.replace(tzinfo=UTC) == run_time

        props = [db.get(CorrectionProposal, pid) for pid in res["proposal_ids"]]
        assert all(p is not None for p in props)
        # Strictly pending, never auto-approved
        assert all(p.status == "pending" for p in props)
        assert all(p.source == "evolution" for p in props)
        for p in props:
            gate = json.loads(p.gate_report or "{}")
            ev = gate["evidence"]
            assert ev["source_type"] == "evolution"
            assert ev["trigger_evidence"]["trace_ids"] == ["tr-a1", "tr-a2"]
            assert ev["trigger_evidence"]["evolution_run_id"] == res["id"]
            assert ev["trigger_evidence"]["llm_rationale"] == p.reason
            assert secret_val not in (p.gate_report or "")

    # Source Git repo MUST remain completely untouched while pending!
    head_now = (
        subprocess.check_output(["git", "-C", str(src), "rev-parse", "HEAD"])
        .decode()
        .strip()
    )
    assert head_now == init_sha

    # Second run: manual historical replay BEFORE watermark with same LLM output -> all deduplicated against pending, watermark NOT moved backward!
    hist_since = wm0 - timedelta(hours=2)
    hist_until = wm0 - timedelta(hours=1)
    res_hist = evolution_runner.run_evolution(
        "projalpha",
        trigger_type="manual",
        since=hist_since,
        until=hist_until,
        llm_fn=fake_llm,
        now=run_time + timedelta(minutes=10),
    )
    assert res_hist["status"] == "succeeded"
    assert res_hist["proposals_created"] == 0
    assert res_hist["duplicates_skipped"] == 4
    with sessions()() as db:
        cfg = db.get(ProjectEvolutionConfig, "projalpha")
        assert cfg.last_trace_timestamp.replace(tzinfo=UTC) == run_time

    # Third run: LLM fails -> run fails and watermark does NOT advance
    def failing_llm(_ctx: dict):
        raise knowledge_synthesizer.LLMUnavailableError("LLM timeout")

    res_fail = evolution_runner.run_evolution(
        "projalpha",
        trigger_type="manual",
        llm_fn=failing_llm,
        now=run_time + timedelta(hours=1),
    )
    assert res_fail["status"] == "failed"
    assert res_fail["stop_reason"] == "llm_unavailable"
    with sessions()() as db:
        cfg = db.get(ProjectEvolutionConfig, "projalpha")
        assert cfg.last_trace_timestamp.replace(tzinfo=UTC) == run_time


def test_watermark_truncation_uses_raw_fetch_limit_and_atomic_scheduled_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _setup_env(tmp_path, monkeypatch)
    from semantica_dataagent import evolution_runner, plugins
    from semantica_dataagent.db import EvolutionRun, ProjectEvolutionConfig, sessions
    from semantica_dataagent.plugins import TraceConversation, TraceMessage

    wm0 = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
    created_at = datetime(2026, 11, 24, 0, 30, tzinfo=UTC)
    run_now = datetime(2026, 11, 24, 5, 0, tzinfo=UTC)

    with sessions().begin() as db:
        db.add(
            ProjectEvolutionConfig(
                project_id="default",
                enabled=True,
                trace_source_plugin="trunc-ts",
                schedule_cron="0 2 * * *",
                timezone="UTC",
                last_trace_timestamp=wm0,
                created_at=created_at,
                updated_at=created_at,
            )
        )

    # 1. Atomic scheduled claim: two claims for the same due slot -> second MUST raise EvolutionConflictError
    # even after first run finishes!
    first_sched = evolution_runner.create_evolution_run(
        "default",
        trigger_type="scheduler",
        created_by="scheduler",
        claim_scheduled_at=run_now,
    )
    with sessions().begin() as db:
        db.get(EvolutionRun, first_sched["id"]).status = "succeeded"
    with pytest.raises(evolution_runner.EvolutionConflictError):
        evolution_runner.create_evolution_run(
            "default",
            trigger_type="scheduler",
            created_by="scheduler",
            claim_scheduled_at=run_now + timedelta(minutes=1),
        )

    # 2. Truncation: raw fetch hits max_traces=2 because one item belongs to another project;
    # filtered conversations has len=1 (<2), yet watermark MUST stop at tr-1's timestamp (01:20), NOT run_now (05:00)!
    t_conv1 = wm0 + timedelta(minutes=20)
    t_other = wm0 + timedelta(minutes=25)

    class _TruncatingTraceSource:
        id = "trunc-ts"
        version = "1.0"
        display_name = "Truncating TS"
        parameters_schema: ClassVar[dict] = {"type": "object", "properties": {}}

        def fetch_traces(self, query):
            if query.since == wm0:
                return [
                    TraceConversation(
                        trace_id="tr-1",
                        project_id="default",
                        started_at=t_conv1,
                        ended_at=t_conv1,
                        messages=[TraceMessage(role="user", content="Q1")],
                    ),
                    TraceConversation(
                        trace_id="tr-other",
                        project_id="other_proj",
                        started_at=t_other,
                        ended_at=t_other,
                        messages=[TraceMessage(role="user", content="OTHER")],
                    ),
                ]
            # Second scenario: hit_limit=True with 0 surviving conversations for target project
            return [
                TraceConversation(
                    trace_id="tr-other-1",
                    project_id="other_proj",
                    started_at=t_conv1 + timedelta(minutes=10),
                    messages=[TraceMessage(role="user", content="OTHER1")],
                ),
                TraceConversation(
                    trace_id="tr-other-2",
                    project_id="other_proj",
                    started_at=t_other + timedelta(minutes=10),
                    messages=[TraceMessage(role="user", content="OTHER2")],
                ),
            ]

    base = dict(plugins.discover())
    base[("trace_source", "trunc-ts")] = _TruncatingTraceSource()
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))

    res1 = evolution_runner.run_evolution(
        "default",
        trigger_type="manual",
        max_traces=2,
        llm_fn=lambda _ctx: {"proposals": []},
        now=run_now,
    )
    assert res1["status"] == "succeeded"
    assert res1["traces_fetched"] == 1
    with sessions()() as db:
        wm1 = db.get(ProjectEvolutionConfig, "default").last_trace_timestamp.replace(
            tzinfo=UTC
        )
        assert wm1 == t_conv1

    # Scenario 2: raw_conversations hits limit=2 with 0 surviving target conversations ->
    # advances to max raw timestamp (t_other + 10m), NOT run_now (05:00)!
    res2 = evolution_runner.run_evolution(
        "default",
        trigger_type="manual",
        max_traces=2,
        llm_fn=lambda _ctx: {"proposals": []},
        now=run_now,
    )
    assert res2["status"] == "succeeded"
    assert res2["traces_fetched"] == 0
    with sessions()() as db:
        wm2 = db.get(ProjectEvolutionConfig, "default").last_trace_timestamp.replace(
            tzinfo=UTC
        )
        assert wm2 == t_other + timedelta(minutes=10)
        assert wm2 < run_now
