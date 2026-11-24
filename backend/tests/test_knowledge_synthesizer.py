"""Unit and integration tests for the unified LLM knowledge synthesizer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.cli import initialize

    return initialize()


def _seed_project(
    project_id: str,
    *,
    project_type: str = "warehouse",
    source_dir: Path | None = None,
) -> None:
    from semantica_dataagent.db import (
        GenerationPointer,
        KnowledgeGeneration,
        Project,
        ProjectSetting,
        sessions,
    )

    with sessions().begin() as db:
        db.add(
            Project(
                id=project_id,
                name=f"Synth {project_id}",
                type=project_type,
                status="active",
            )
        )
        gen = f"{project_id}_g1"
        db.add(KnowledgeGeneration(name=gen, project_id=project_id, build_id=1))
        db.add(GenerationPointer(project_id=project_id, generation=gen))
        if source_dir is not None:
            source_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "-C", str(source_dir), "init", "-q"], check=True)
            db.add(
                ProjectSetting(
                    project_id=project_id,
                    key="source_directory",
                    value=str(source_dir),
                )
            )


def _mock_warehouse_graph(
    monkeypatch: pytest.MonkeyPatch, docs: list[dict], tables: list[dict]
):
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


def test_synthesizer_auto_routes_override_existing_md_and_new_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge_synthesizer

    _setup_env(tmp_path, monkeypatch)
    src = tmp_path / "src_route"
    _seed_project("projroute", project_type="warehouse", source_dir=src)
    (src / "knowledge" / "metrics").mkdir(parents=True)
    (src / "knowledge" / "metrics" / "gmv.md").write_text(
        "# GMV 口径\n\n统计已支付订单金额。\n", encoding="utf-8"
    )

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
                {"name": "id", "type": "bigint", "comment": "主键"},
                {"name": "status", "type": "string", "comment": "状态"},
                {"name": "amount", "type": "decimal", "comment": "金额"},
            ],
        },
        {
            "database": "shop",
            "name": "users",
            "comment": "用户表",
            "fields": [{"name": "id", "type": "bigint", "comment": "用户ID"}],
        },
    ]
    _mock_warehouse_graph(monkeypatch, docs, tables)

    captured: list[dict] = []

    def stub_llm(ctx: dict) -> dict:
        captured.append(ctx)
        desc = ctx["description"]
        if "新规则文档" in desc:
            return {
                "route": "new_document",
                "new_doc_path": "knowledge/rules/cross_table_refund.md",
                "reason": "新建跨表退款核销规则文档",
                "patch": "# 跨表退款核销规则\n\n关联 orders 与 refunds 表计算净额。\n",
            }
        if "GMV" in desc:
            return {
                "route": "existing_document",
                "target": "document:metrics/gmv.md",
                "reason": "更新 GMV 已有口径文档补充剔除测试账号",
                "patch": "# GMV 口径\n\n统计已支付订单金额。\n\n## 过滤条件\n\n剔除测试账号。\n",
            }
        return {
            "route": "override",
            "reason": "补充订单状态字段同义词与约束",
            "structured_patch": {
                "version": 1,
                "fields": {
                    "shop.orders.status": {
                        "database": "shop",
                        "table": "orders",
                        "name": "status",
                        "synonyms": ["订单状态码"],
                        "instructions": "仅统计 paid 状态",
                    }
                },
            },
        }

    # 1) Single field constraint -> overrides/semantic_overrides.yaml
    res_override = knowledge_synthesizer.synthesize_knowledge(
        project_id="projroute",
        source_type="evolution",
        description="shop.orders.status 别名叫订单状态码，统计只看 paid",
        raw_evidence={"trace_ids": ["tr-1"]},
        llm_fn=stub_llm,
    )
    assert res_override.target == "field:shop.orders.status"
    assert res_override.target_kind == "override"
    assert res_override.resolved_path == "overrides/semantic_overrides.yaml"
    assert res_override.is_new_file is False
    assert res_override.gate_report["passed"] is True
    assert res_override.evidence["source_type"] == "evolution"
    assert res_override.evidence["trigger_evidence"] == {"trace_ids": ["tr-1"]}
    assert captured[0]["tables"] and captured[0]["documents"]

    # 2) Topic caliber -> merge into existing Markdown document
    res_existing = knowledge_synthesizer.synthesize_knowledge(
        project_id="projroute",
        source_type="manual",
        description="GMV 口径需要剔除测试账号",
        llm_fn=stub_llm,
    )
    assert res_existing.target == "document:metrics/gmv.md"
    assert res_existing.target_kind == "document"
    assert res_existing.resolved_path == "knowledge/metrics/gmv.md"
    assert res_existing.is_new_file is False
    assert "+剔除测试账号。" in res_existing.diff
    assert res_existing.gate_report["passed"] is True

    # 3) No matching doc -> create new Markdown document with /dev/null -> b/... diff
    res_new = knowledge_synthesizer.synthesize_knowledge(
        project_id="projroute",
        source_type="discovery",
        description="沉淀新规则文档：跨表退款核销规则",
        llm_fn=stub_llm,
    )
    assert res_new.target == "document:knowledge/rules/cross_table_refund.md"
    assert res_new.target_kind == "document"
    assert res_new.resolved_path == "knowledge/rules/cross_table_refund.md"
    assert res_new.is_new_file is True
    assert "--- /dev/null" in res_new.diff
    assert "+++ b/knowledge/rules/cross_table_refund.md" in res_new.diff
    assert res_new.gate_report["passed"] is True


def test_synthesizer_anti_duplication_against_existing_and_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge_synthesizer
    from semantica_dataagent.db import CorrectionProposal, sessions

    _setup_env(tmp_path, monkeypatch)
    src = tmp_path / "src_dup"
    _seed_project("projdup", project_type="warehouse", source_dir=src)
    (src / "overrides").mkdir(parents=True)
    (src / "overrides" / "semantic_overrides.yaml").write_text(
        "version: 1\ntables:\n  shop.orders:\n    database: shop\n    name: orders\n    comment: 订单主表\n",
        encoding="utf-8",
    )
    (src / "knowledge" / "metrics").mkdir(parents=True)
    (src / "knowledge" / "metrics" / "gmv.md").write_text(
        "# GMV\n\n已包含退款剔除规则。\n", encoding="utf-8"
    )
    tables = [
        {"database": "shop", "name": "orders", "fields": [{"name": "amount"}]},
    ]
    _mock_warehouse_graph(monkeypatch, [], tables)

    # Seed a pending proposal for metric daily_gmv
    with sessions().begin() as db:
        db.add(
            CorrectionProposal(
                id="pend-1",
                project_id="projdup",
                owner_id="u1",
                target="metric:daily_gmv",
                patch="version: 1\n",
                original="",
                diff="diff",
                reason="新增日 GMV 指标",
                status="pending",
                source="evolution",
                target_kind="override",
                structured_patch=json.dumps(
                    {
                        "version": 1,
                        "metrics": {
                            "daily_gmv": {
                                "name": "daily_gmv",
                                "expression": "SUM(orders.amount)",
                                "dialect": "doris",
                                "tables": ["shop.orders"],
                            }
                        },
                    }
                ),
            )
        )

    def batch_llm(_ctx: dict) -> dict:
        return {
            "proposals": [
                # Duplicate of existing semantic_overrides.yaml
                {
                    "route": "override",
                    "reason": "重复表注释",
                    "structured_patch": {
                        "version": 1,
                        "tables": {
                            "shop.orders": {
                                "database": "shop",
                                "name": "orders",
                                "comment": "订单主表",
                            }
                        },
                    },
                },
                # Duplicate of pending proposal pend-1
                {
                    "route": "override",
                    "reason": "再次提交同指标",
                    "structured_patch": {
                        "version": 1,
                        "metrics": {
                            "daily_gmv": {
                                "name": "daily_gmv",
                                "expression": "SUM(orders.amount)",
                                "dialect": "doris",
                                "tables": ["shop.orders"],
                            }
                        },
                    },
                },
                # Duplicate of existing markdown content
                {
                    "route": "existing_document",
                    "target": "document:metrics/gmv.md",
                    "reason": "重复文档内容",
                    "patch": "# GMV\n\n已包含退款剔除规则。\n",
                },
                # Novel valid proposal
                {
                    "route": "override",
                    "reason": "新增实付金额说明",
                    "structured_patch": {
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
                },
            ]
        }

    all_items = knowledge_synthesizer.synthesize_proposals(
        project_id="projdup",
        source_type="discovery",
        description="批量归纳 SQL 模板",
        allow_multi=True,
        skip_duplicates=False,
        llm_fn=batch_llm,
    )
    assert len(all_items) == 4
    assert all_items[0].is_duplicate is True
    assert all_items[0].duplicate_of == "existing_knowledge"
    assert all_items[1].is_duplicate is True
    assert all_items[1].duplicate_of == "pending_proposal:pend-1"
    assert all_items[2].is_duplicate is True
    assert all_items[2].duplicate_of == "existing_knowledge"
    assert all_items[3].is_duplicate is False

    filtered = knowledge_synthesizer.synthesize_proposals(
        project_id="projdup",
        source_type="discovery",
        description="批量归纳 SQL 模板",
        allow_multi=True,
        skip_duplicates=True,
        llm_fn=batch_llm,
    )
    assert len(filtered) == 1
    assert filtered[0].target == "field:shop.orders.amount"


def test_synthesizer_l1_rejects_bad_schema_and_unsafe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge_synthesizer

    _setup_env(tmp_path, monkeypatch)
    src = tmp_path / "src_l1"
    _seed_project("projl1", project_type="warehouse", source_dir=src)
    tables = [{"database": "shop", "name": "orders", "fields": [{"name": "amount"}]}]
    _mock_warehouse_graph(monkeypatch, [], tables)

    # Nonexistent column in physical schema
    bad_field = knowledge_synthesizer.synthesize_knowledge(
        project_id="projl1",
        source_type="manual",
        description="不存在的列",
        llm_fn=lambda _c: {
            "route": "override",
            "reason": "bad field",
            "structured_patch": {
                "version": 1,
                "fields": {
                    "shop.orders.nonexistent": {
                        "database": "shop",
                        "table": "orders",
                        "name": "nonexistent",
                        "comment": "x",
                    }
                },
            },
        },
    )
    assert bad_field.gate_report["passed"] is False
    assert any("does not exist" in e for e in bad_field.gate_report["errors"])

    # Path traversal in new document
    bad_path = knowledge_synthesizer.synthesize_knowledge(
        project_id="projl1",
        source_type="manual",
        description="路径穿越",
        llm_fn=lambda _c: {
            "route": "new_document",
            "new_doc_path": "knowledge/../etc/passwd.md",
            "reason": "bad path",
            "patch": "# evil\n",
        },
    )
    assert bad_path.gate_report["passed"] is False

    # Non-markdown extension in new document
    bad_ext = knowledge_synthesizer.synthesize_knowledge(
        project_id="projl1",
        source_type="manual",
        description="非md文件",
        llm_fn=lambda _c: {
            "route": "new_document",
            "new_doc_path": "knowledge/rules/script.py",
            "reason": "bad ext",
            "patch": "print(1)\n",
        },
    )
    assert bad_ext.gate_report["passed"] is False


def test_synthesizer_resolves_default_and_override_providers_and_raises_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import knowledge_synthesizer
    from semantica_dataagent.crypto import encrypt
    from semantica_dataagent.db import Provider, Setting, sessions

    _setup_env(tmp_path, monkeypatch)
    src = tmp_path / "src_prov"
    _seed_project("projprov", project_type="document", source_dir=src)
    _mock_warehouse_graph(monkeypatch, [], [])

    # 1) Unconfigured -> LLMUnavailableError
    with pytest.raises(knowledge_synthesizer.LLMUnavailableError) as exc_info:
        knowledge_synthesizer.synthesize_knowledge(
            project_id="projprov",
            source_type="manual",
            description="没有模型",
        )
    assert exc_info.value.code == "llm_unavailable"
    assert exc_info.value.manual_edit_recommended is True

    # 2) Seed two providers and set p2 as assistant default
    with sessions().begin() as db:
        db.add(
            Provider(
                id="p1",
                name="A-OpenAI",
                type="openai-completions",
                base_url="http://127.0.0.1:9991",
                api_key=encrypt("key-1"),
                models=json.dumps(["gpt-4o", "gpt-4o-mini"]),
            )
        )
        db.add(
            Provider(
                id="p2",
                name="B-Anthropic",
                type="anthropic-messages",
                base_url="http://127.0.0.1:9992",
                api_key=encrypt("key-2"),
                models=json.dumps(["claude-sonnet"]),
            )
        )
        db.add(Setting(key="assistant_provider_id", value="p2"))
        db.add(Setting(key="assistant_model", value="claude-sonnet"))

    resolved_default = knowledge_synthesizer.resolve_llm_provider("projprov")
    assert resolved_default is not None
    assert resolved_default["id"] == "p2"
    assert resolved_default["model"] == "claude-sonnet"
    assert resolved_default["api_key"] == "key-2"

    resolved_override = knowledge_synthesizer.resolve_llm_provider(
        "projprov", provider_id="p1", model="gpt-4o-mini"
    )
    assert resolved_override is not None
    assert resolved_override["id"] == "p1"
    assert resolved_override["model"] == "gpt-4o-mini"

    # 3) Test generate_with_llm via mocked httpx.post for openai-completions
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "route": "new_document",
                                    "new_doc_path": "knowledge/faq/guide.md",
                                    "reason": "整理文档型项目指南",
                                    "patch": "# 指南\n\n规范说明。\n",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(
        knowledge_synthesizer.httpx, "post", lambda *a, **kw: FakeResp()
    )
    out = knowledge_synthesizer.synthesize_knowledge(
        project_id="projprov",
        source_type="manual",
        description="整理平台使用规范",
        provider_id="p1",
        model="gpt-4o-mini",
    )
    assert out.used_llm is True
    assert out.is_new_file is True
    assert out.target == "document:knowledge/faq/guide.md"
    assert out.resolved_path == "knowledge/faq/guide.md"
