"""Tests for ADR-0014 historical tickets, LLM context curation, anti-leakage, and source/destination tracking."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def _configure(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    from semantica_dataagent.cli import initialize

    return initialize()


def _create_user_token(name: str) -> tuple[str, str]:
    from semantica_dataagent.db import Token, digest, sessions

    plaintext = secrets.token_urlsafe(24)
    with sessions().begin() as db:
        row = Token(digest=digest(plaintext), name=name, role="user")
        db.add(row)
        db.flush()
        return plaintext, row.id


def _seed_multiturn_conversation(project_id: str, owner_id: str) -> tuple[str, int]:
    from semantica_dataagent.db import (
        Clarification,
        Conversation,
        Turn,
        TurnEvent,
        sessions,
    )

    conv_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    with sessions().begin() as db:
        db.add(
            Conversation(
                id=conv_id,
                project_id=project_id,
                owner_id=owner_id,
                title="机票销售额多轮追问",
                agent="pi",
                provider_id="p1",
                model="m1",
                created_at=now,
                updated_at=now,
            )
        )
        t1 = Turn(
            conversation_id=conv_id,
            provider_id="p1",
            model="m1",
            effort="medium",
            sequence=1,
            question="查一下今年航司销售额",
            status="completed",
            answer="请问按支付成功时间还是出票完成时间？",
            sql="[]",
            citations="[]",
            started_at=now,
            finished_at=now,
        )
        t2 = Turn(
            conversation_id=conv_id,
            provider_id="p1",
            model="m1",
            effort="medium",
            sequence=2,
            question="按 finished_time，只要已出票且排除辅营产品",
            status="completed",
            answer="已为您统计今年按 finished_time 的机票销售额。",
            sql=json.dumps(
                [
                    {
                        "sql": "SELECT SUM(gmv_cny) FROM dm.dm_air_orders WHERE day_num = '2026-11-26' AND status = 400",
                        "dialect": "doris",
                    }
                ]
            ),
            citations="[]",
            started_at=now,
            finished_at=now,
        )
        db.add_all([t1, t2])
        db.flush()
        db.add(
            Clarification(
                owner_id=owner_id,
                conversation_id=conv_id,
                questions=json.dumps(
                    [
                        {
                            "question": "销售额统计时间基准",
                            "answer": "finished_time",
                        }
                    ],
                    ensure_ascii=False,
                ),
                status="resolved",
                created_at=now,
            )
        )
        db.add(
            TurnEvent(
                turn_id=t2.id,
                seq=0,
                type="tool_end",
                data=json.dumps(
                    {
                        "name": "mcp__semantica__validate_sql",
                        "is_error": False,
                        "summary": "status=pass errors=0 warnings=0",
                    }
                ),
            )
        )
        db.flush()
        return conv_id, t2.id


def test_multiturn_feedback_llm_synthesis_approval_and_crud(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent import (
        eval_questions,
        knowledge_synthesizer,
        project_context,
        sql_validation,
    )
    from semantica_dataagent.app import app
    from semantica_dataagent.crypto import encrypt
    from semantica_dataagent.db import Provider, sessions
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Ticket Warehouse", "warehouse")
    member_token, member_id = _create_user_token("ticket-member")
    member_headers = {"Authorization": f"Bearer {member_token}"}

    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="hive", project_id=None: {"status": "pass", "issues": []},
    )

    captured_payloads: list[dict] = []

    def fake_invoke_llm(provider_cfg, system_prompt, payload_data):
        captured_payloads.append(dict(payload_data))
        return {
            "route": "new_document",
            "slug": "air-ticket-gmv-finished-time",
            "target": "document:knowledge/historical-tickets/air-ticket-gmv-finished-time.md",
            "title": "今年机票按 finished_time 统计已出票销售额（排除辅营）",
            "reason": "结合两轮对话补充的 finished_time 与排除辅营口径，提炼为标准历史工单",
            "patch": (
                "# 今年机票按 finished_time 统计已出票销售额（排除辅营）\n\n"
                "- **来源**: 对话点赞沉淀\n"
                "- **涉及表**: dm.dm_air_orders\n"
                "- **方言**: doris\n\n"
                "## 业务口径与上下文\n\n"
                "按 finished_time 归属业务时间，筛选 status=400 已出票且排除辅营产品。\n\n"
                "## 参考 SQL\n\n"
                "```sql\n"
                "SELECT SUM(gmv_cny) FROM dm.dm_air_orders WHERE day_num = '2026-11-26' AND status = 400\n"
                "```\n\n"
                "## 注意事项与避坑点\n\n"
                "不要误用 paid_time。\n"
            ),
        }

    monkeypatch.setattr(knowledge_synthesizer, "invoke_llm_json", fake_invoke_llm)

    with sessions().begin() as db:
        db.add(
            Provider(
                id="p1",
                name="TestProvider",
                type="openai-completions",
                base_url="https://llm.example.com",
                api_key=encrypt("sk-test-secret"),
                models=json.dumps(["m1"]),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    with TestClient(app, headers=headers) as client:
        assert (
            client.put(
                f"/api/v1/projects/{project.id}/members/{member_id}",
                json={"role": "member"},
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/v1/projects/{project.id}/auto-build",
                json={"enabled": False},
            ).status_code
            == 200
        )

        conv_id, turn_id = _seed_multiturn_conversation(project.id, member_id)

        # Seed a benchmark eval question that must NOT be overwritten and blocks leakage
        client.post(
            "/api/v1/eval-questions",
            params={"project": project.id},
            json={
                "id": "bench-1",
                "question": "保留基准问题：昨日支付单量",
                "caliber": "按 paid_time 统计",
                "reference_sql": "SELECT COUNT(*) FROM dm.dm_paid_orders",
                "dialect": "doris",
                "split": "holdout",
                "answer_confirmed": True,
            },
        )

        fb = client.post(
            f"/api/v1/conversations/{conv_id}/turns/{turn_id}/feedback",
            headers=member_headers,
            json={"rating": "accurate"},
        )
        assert fb.status_code == 201, fb.text
        fb_body = fb.json()
        assert fb_body["proposal_status"] == "pending"
        assert fb_body["destination_kind"] == "historical_ticket"
        assert fb_body["synthesis_mode"] == "llm"
        proposal_id = fb_body["proposal_id"]

        # Verify multi-turn context was forwarded to LLM synthesizer
        assert len(captured_payloads) == 1
        ctx_turns = captured_payloads[0]["conversation_turns"]
        assert len(ctx_turns) == 2
        assert ctx_turns[0]["question"] == "查一下今年航司销售额"
        assert captured_payloads[0]["clarifications"][0]["answer"] == "finished_time"

        # Eval questions file MUST remain untouched (still only bench-1)
        assert len(eval_questions._read_raw_rows(project.id)[1]) == 1

        # Corrections list marks explicit source=feedback and destination_kind=historical_ticket
        corr_res = client.get(
            "/api/v1/corrections",
            params={
                "project": project.id,
                "source": "feedback",
                "destination_kind": "historical_ticket",
            },
        )
        assert corr_res.status_code == 200
        corr_items = corr_res.json()
        assert len(corr_items) == 1
        assert corr_items[0]["id"] == proposal_id
        assert corr_items[0]["source"] == "feedback"
        assert corr_items[0]["destination_kind"] == "historical_ticket"

        # Historical tickets endpoint shows pending proposal count = 1, indexed items = 0
        tickets_before = client.get(
            "/api/v1/historical-tickets", params={"project": project.id}
        )
        assert tickets_before.status_code == 200
        assert tickets_before.json()["pending_proposal_count"] == 1
        assert tickets_before.json()["items"] == []

        # Approve proposal -> writes knowledge/historical-tickets/*.md, does NOT touch eval/questions.yaml
        app_res = client.post(
            f"/api/v1/corrections/{proposal_id}/approve",
            params={"project": project.id},
        )
        assert app_res.status_code == 200, app_res.text

        # Approved feedback cannot be deleted via conversation feedback DELETE
        assert (
            client.delete(
                f"/api/v1/conversations/{conv_id}/turns/{turn_id}/feedback",
                headers=member_headers,
            ).status_code
            == 409
        )

        # Eval questions still only bench-1
        assert len(eval_questions._read_raw_rows(project.id)[1]) == 1

        # Historical tickets API lists the newly approved ticket
        tickets_after = client.get(
            "/api/v1/historical-tickets", params={"project": project.id}
        )
        assert tickets_after.status_code == 200
        t_data = tickets_after.json()
        assert t_data["pending_proposal_count"] == 0
        assert len(t_data["items"]) == 1
        assert t_data["items"][0]["slug"] == "air-ticket-gmv-finished-time"
        assert "dm.dm_air_orders" in t_data["items"][0]["tables"]

        # Member cannot create/update/delete historical tickets manually (403)
        assert (
            client.post(
                "/api/v1/historical-tickets",
                params={"project": project.id},
                headers=member_headers,
                json={"title": "member test", "reference_sql": "SELECT 1"},
            ).status_code
            == 403
        )

        # Anti-leakage guard blocks creating ticket matching eval benchmark bench-1 (409)
        leak_create = client.post(
            "/api/v1/historical-tickets",
            params={"project": project.id},
            json={
                "title": "昨日支付单量统计",
                "caliber": "重复评测题",
                "reference_sql": "SELECT COUNT(*) FROM dm.dm_paid_orders",
                "dialect": "doris",
            },
        )
        assert leak_create.status_code == 409

        # Admin manual CRUD (POST / PATCH / DELETE)
        created = client.post(
            "/api/v1/historical-tickets",
            params={"project": project.id},
            json={
                "title": "人工录入历史工单测试",
                "caliber": "只统计中国区",
                "reference_sql": "SELECT COUNT(1) FROM dm.dm_manual_tbl WHERE region = 'CN'",
                "dialect": "doris",
                "caveats": "注意时区",
            },
        )
        assert created.status_code == 201, created.text
        manual_slug = created.json()["slug"]

        patched = client.patch(
            f"/api/v1/historical-tickets/{manual_slug}",
            params={"project": project.id},
            json={"caveats": "注意 UTC+8 时区转换"},
        )
        assert patched.status_code == 200
        assert "UTC+8" in patched.json()["content"]

        deleted = client.delete(
            f"/api/v1/historical-tickets/{manual_slug}",
            params={"project": project.id},
        )
        assert deleted.status_code == 204
        assert (
            len(
                client.get(
                    "/api/v1/historical-tickets", params={"project": project.id}
                ).json()["items"]
            )
            == 1
        )
        assert project_context.project_source_directory(project.id).is_dir()


def test_feedback_fallback_without_llm_and_leakage_block(tmp_path: Path, monkeypatch):
    from semantica_dataagent import knowledge_synthesizer, sql_validation
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Fallback Ticket", "warehouse")

    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="hive", project_id=None: {"status": "pass", "issues": []},
    )
    monkeypatch.setattr(
        knowledge_synthesizer, "resolve_llm_provider", lambda *args, **kwargs: None
    )

    from semantica_dataagent.db import Token, digest, sessions

    with sessions()() as db:
        admin_id = db.get(Token, digest(admin_token)).id
    conv_id, turn_id = _seed_multiturn_conversation(project.id, admin_id)

    with TestClient(app, headers=headers) as client:
        # Without LLM configured, feedback still creates structured fallback proposal including prior context
        fb = client.post(
            f"/api/v1/conversations/{conv_id}/turns/{turn_id}/feedback",
            json={"rating": "accurate"},
        )
        assert fb.status_code == 201, fb.text
        assert fb.json()["synthesis_mode"] == "fallback"

        prop_detail = client.get(
            f"/api/v1/corrections/{fb.json()['proposal_id']}",
            params={"project": project.id},
        ).json()
        assert prop_detail["source"] == "feedback"
        assert prop_detail["destination_kind"] == "historical_ticket"
        assert "查一下今年航司销售额" in prop_detail["patch"]
        assert "finished_time" in prop_detail["patch"]

        # Revoke works cleanly while pending
        assert (
            client.delete(
                f"/api/v1/conversations/{conv_id}/turns/{turn_id}/feedback"
            ).status_code
            == 204
        )

        # If matching an eval benchmark question, feedback is rejected with 409 anti-leakage
        client.post(
            "/api/v1/eval-questions",
            params={"project": project.id},
            json={
                "id": "bench-dup",
                "question": "按 finished_time，只要已出票且排除辅营产品",
                "caliber": "基准题",
                "reference_sql": "SELECT SUM(gmv_cny) FROM dm.dm_air_orders WHERE day_num = '2026-11-26' AND status = 400",
                "dialect": "doris",
                "split": "holdout",
                "answer_confirmed": True,
            },
        )
        blocked = client.post(
            f"/api/v1/conversations/{conv_id}/turns/{turn_id}/feedback",
            json={"rating": "accurate"},
        )
        assert blocked.status_code == 409


PLACEHOLDER_LEGAL_TEXTS = {
    "prose_english": (
        "Note: replace the placeholder value with a real business date before running."
    ),
    "prose_chinese": (
        "说明：SQL 中的 placeholder 仅作示例说明，实际查询请填写具体日期。"
    ),
    "column_name": (
        "SELECT order_id, placeholder_flag FROM dm.dm_orders WHERE dt = '2026-11-26'"
    ),
    "sql_line_comment": (
        "-- placeholder: filter out test orders\nSELECT COUNT(1) FROM dm.dm_orders"
    ),
    "sql_block_comment": (
        "/* placeholder comment for future filter */\nSELECT COUNT(1) FROM dm.dm_orders"
    ),
    "string_literal": "SELECT * FROM dm.dm_orders WHERE tag = 'placeholder'",
    "string_literal_phrase": (
        "SELECT * FROM dm.dm_orders WHERE note = 'default placeholder text'"
    ),
    "realistic_ticket": (
        "# 过滤占位订单统计真实成交额\n\n"
        "- **来源**: 对话点赞沉淀\n"
        "- **涉及表**: dm.dm_orders\n"
        "- **方言**: doris\n\n"
        "## 业务口径与上下文\n\n"
        "排除 `placeholder_flag = 1` 的测试占位记录。\n\n"
        "## 参考 SQL\n\n"
        "```sql\n"
        "SELECT SUM(amount) FROM dm.dm_orders WHERE dt = '2026-11-26' "
        "AND placeholder_flag = 0\n"
        "```\n"
    ),
}


def test_placeholder_detector_covers_common_deformations():
    from semantica_dataagent.historical_tickets import find_unresolved_placeholders

    positives = {
        "exact": "__PLACEHOLDER__",
        "lowercase": "__placeholder__",
        "titlecase": "__Placeholder__",
        "bare_token": "PLACEHOLDER",
        "bare_token_in_string": "SELECT 'PLACEHOLDER' FROM t",
        "prefix_suffix": "__PLACEHOLDER_DATE__",
        "hyphen_suffix": "__PLACEHOLDER-DATE__",
        "digit_suffix": "__PLACEHOLDER2__",
        "custom_prefix": "__MY_PLACEHOLDER_V2__",
        "inside_string": "'__PLACEHOLDER__'",
        "inside_double_quotes": '"__placeholder__"',
        "inside_comment": "-- __PLACEHOLDER__",
        "inside_block_comment": "/* __PLACEHOLDER_DATE__ */",
        "spaces": "__ PLACEHOLDER __",
        "extra_wrapping_underscores": "___PLACEHOLDER___",
    }
    for name, text in positives.items():
        assert find_unresolved_placeholders(text), name

    negatives = {
        "bare_lowercase": "placeholder",
        "bare_mixed_case": "Placeholder",
        "bare_uppercase_suffix": "PLACEHOLDER_DATE",
        "bare_prefixed": "my_PLACEHOLDER_value",
        "single_underscore": "_placeholder_",
        "prefixed_identifier": "my_placeholder_1",
        "is_placeholder": "is_placeholder",
        "placeholder_suffix_word": "placeholderish",
        "inside_string_bare_suffix": "SELECT 'placeholder_value' FROM t",
        "dunder_identifier": "SELECT __init__ FROM t",
        "ordinary_identifiers": (
            "SELECT order_id, __internal_flag FROM shop.orders "
            "WHERE dt = '2025-01-01' -- 按分区过滤"
        ),
        **PLACEHOLDER_LEGAL_TEXTS,
    }
    for name, text in negatives.items():
        assert find_unresolved_placeholders(text) == [], name


def test_placeholder_detector_does_not_flag_existing_ticket_samples():
    from semantica_dataagent.historical_tickets import find_unresolved_placeholders

    root = Path(__file__).resolve().parents[2]
    sample_files = sorted((root / "samples" / "demo").rglob("*.md"))
    assert sample_files, (
        "expected existing Markdown samples as the false-positive corpus"
    )
    flagged = {
        str(path.relative_to(root)): find_unresolved_placeholders(
            path.read_text(encoding="utf-8")
        )
        for path in sample_files
    }
    assert {path: markers for path, markers in flagged.items() if markers} == {}

    realistic_ticket = (
        "# 今年机票按 finished_time 统计已出票销售额（排除辅营）\n\n"
        "- **来源**: 对话点赞沉淀\n"
        "- **涉及表**: dm.dm_air_orders\n"
        "- **方言**: doris\n\n"
        "## 业务口径与上下文\n\n"
        "按 finished_time 归属业务时间，筛选 `status = 400` 已出票且排除辅营产品。\n\n"
        "## 参考 SQL\n\n"
        "```sql\n"
        "-- 只统计出票完成、且非辅营的订单\n"
        "SELECT SUM(gmv_cny)\n"
        "FROM dm.dm_air_orders\n"
        "WHERE day_num = '2026-11-26'\n"
        "  AND status = 400\n"
        "  AND product_type <> '__EXCLUDED_PRODUCT__'\n"
        "```\n\n"
        "## 注意事项与避坑点\n\n不要误用 paid_time。\n"
    )
    assert find_unresolved_placeholders(realistic_ticket) == []


def test_placeholder_gate_is_scoped_to_historical_ticket_targets():
    from semantica_dataagent.historical_tickets import (
        historical_ticket_placeholder_errors,
    )

    assert (
        historical_ticket_placeholder_errors(
            "document:knowledge/rules/placeholder_policy.md", "__PLACEHOLDER__"
        )
        == []
    )
    assert historical_ticket_placeholder_errors(
        "document:knowledge/historical-tickets/placeholder-draft.md", "__PLACEHOLDER__"
    )


def test_placeholder_ticket_cannot_be_created_or_approved_via_http(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent.app import app
    from semantica_dataagent.db import CorrectionProposal, sessions
    from semantica_dataagent.project_api import create_project_record
    from semantica_dataagent.project_context import project_source_directory

    admin_token = _configure(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Placeholder Ticket Warehouse", "warehouse")
    source = project_source_directory(project.id)

    variants = {
        "exact": "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__PLACEHOLDER__'",
        "lowercase": "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__placeholder__'",
        "bare": "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = 'PLACEHOLDER'",
        "prefix_suffix": (
            "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__PLACEHOLDER_DATE__'"
        ),
        "inside_string": (
            "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__PLACEHOLDER__' "
            "AND channel = '__PLACEHOLDER__'"
        ),
        "inside_comment": (
            "-- 待替换 __PLACEHOLDER__\n"
            "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '2025-01-01'"
        ),
    }

    with TestClient(app, headers=headers) as client:
        assert (
            client.put(
                f"/api/v1/projects/{project.id}/auto-build",
                json={"enabled": False},
            ).status_code
            == 200
        )

        for name, sql in variants.items():
            patch = f"# 占位草稿 {name}\n\n## 参考 SQL\n\n```sql\n{sql}\n```\n"
            draft_slug = f"placeholder-draft-{name}"
            response = client.post(
                "/api/v1/corrections",
                params={"project": project.id},
                json={
                    "target": (
                        f"document:knowledge/historical-tickets/{draft_slug}.md"
                    ),
                    "target_kind": "document",
                    "patch": patch,
                    "reason": f"占位符变形样例 {name}",
                },
            )
            assert response.status_code == 201, (name, response.text)
            draft_id = response.json()["id"]
            assert response.json()["status"] == "pending"

            baseline_dest = (
                source / "knowledge" / "historical-tickets" / f"{draft_slug}.md"
            )
            approval = client.post(
                f"/api/v1/corrections/{draft_id}/approve",
                params={"project": project.id},
            )
            assert approval.status_code == 422, (name, approval.text)
            assert "占位符" in approval.text, (name, approval.text)
            assert not baseline_dest.exists()
            pending = client.get(
                f"/api/v1/corrections/{draft_id}", params={"project": project.id}
            )
            assert pending.status_code == 200
            assert pending.json()["status"] == "pending"
            assert (
                client.post(
                    f"/api/v1/corrections/{draft_id}/reject",
                    params={"project": project.id},
                ).status_code
                == 200
            )

        legal_approved = 0
        for name, text in PLACEHOLDER_LEGAL_TEXTS.items():
            legal_slug = f"legal-placeholder-{name}"
            legal_patch = f"# 合法历史工单 {name}\n\n{text}\n"
            legal_created = client.post(
                "/api/v1/corrections",
                params={"project": project.id},
                json={
                    "target": (
                        f"document:knowledge/historical-tickets/{legal_slug}.md"
                    ),
                    "target_kind": "document",
                    "patch": legal_patch,
                    "reason": f"合法 placeholder 文本样例 {name}",
                },
            )
            assert legal_created.status_code == 201, (name, legal_created.text)
            legal_id = legal_created.json()["id"]
            legal_approval = client.post(
                f"/api/v1/corrections/{legal_id}/approve",
                params={"project": project.id},
            )
            assert legal_approval.status_code == 200, (name, legal_approval.text)
            legal_dest = (
                source / "knowledge" / "historical-tickets" / f"{legal_slug}.md"
            )
            assert legal_dest.is_file(), name
            assert legal_dest.read_text(encoding="utf-8") == legal_patch, name
            legal_approved += 1
        assert legal_approved == len(PLACEHOLDER_LEGAL_TEXTS)

        listing = client.get("/api/v1/corrections", params={"project": project.id})
        assert listing.status_code == 200
        assert [item for item in listing.json() if item["status"] == "pending"] == []

        valid_patch = (
            "# 正常历史工单\n\n"
            "## 参考 SQL\n\n"
            "```sql\n"
            "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '2025-01-01'\n"
            "```\n"
        )
        created = client.post(
            "/api/v1/corrections",
            params={"project": project.id},
            json={
                "target": "document:knowledge/historical-tickets/valid-placeholder-check.md",
                "target_kind": "document",
                "patch": valid_patch,
                "reason": "合法历史工单用于对比",
            },
        )
        assert created.status_code == 201, created.text
        proposal_id = created.json()["id"]
        dest = (
            source / "knowledge" / "historical-tickets" / "valid-placeholder-check.md"
        )
        assert not dest.exists()

        # Simulate a legacy pending draft that bypassed the creation gate.
        with sessions().begin() as db:
            row = db.get(CorrectionProposal, proposal_id)
            assert row is not None
            row.patch = valid_patch.replace("2025-01-01", "__PLACEHOLDER__")

        rejected = client.post(
            f"/api/v1/corrections/{proposal_id}/approve",
            params={"project": project.id},
        )
        assert rejected.status_code == 422, rejected.text
        assert "占位符" in rejected.text
        assert not dest.exists()

        with sessions().begin() as db:
            row = db.get(CorrectionProposal, proposal_id)
            assert row is not None
            assert row.status == "pending"
            row.patch = valid_patch

        approved = client.post(
            f"/api/v1/corrections/{proposal_id}/approve",
            params={"project": project.id},
        )
        assert approved.status_code == 200, approved.text
        assert dest.is_file()
        assert "__PLACEHOLDER__" not in dest.read_text(encoding="utf-8")

        manual_create = client.post(
            "/api/v1/historical-tickets",
            params={"project": project.id},
            json={
                "title": "人工占位草稿",
                "caliber": "测试占位符拒绝",
                "reference_sql": (
                    "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__PLACEHOLDER__'"
                ),
                "dialect": "doris",
            },
        )
        assert manual_create.status_code == 422, manual_create.text
        assert "占位符" in manual_create.text

        manual_patch = client.patch(
            "/api/v1/historical-tickets/valid-placeholder-check",
            params={"project": project.id},
            json={
                "content": (
                    "# 正常历史工单\n\n"
                    "## 参考 SQL\n\n"
                    "```sql\n"
                    "SELECT COUNT(1) FROM dm.dm_orders "
                    "WHERE dt = '__PLACEHOLDER__'\n"
                    "```\n"
                )
            },
        )
        assert manual_patch.status_code == 422, manual_patch.text
        assert "占位符" in manual_patch.text
        assert "__PLACEHOLDER__" not in dest.read_text(encoding="utf-8")
