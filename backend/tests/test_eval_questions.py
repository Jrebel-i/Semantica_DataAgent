"""Tests for source-isolated per-project eval questions API and lifecycle."""

from __future__ import annotations

import subprocess
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
    import secrets

    from semantica_dataagent.db import Token, digest, sessions

    plaintext = secrets.token_urlsafe(24)
    with sessions().begin() as db:
        row = Token(digest=digest(plaintext), name=name, role="user")
        db.add(row)
        db.flush()
        return plaintext, row.id


def test_eval_questions_source_isolation_and_crud_redaction(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent import eval as eval_mod
    from semantica_dataagent import project_context
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Eval Warehouse", "warehouse")

    member_token, member_id = _create_user_token("eval-member")
    outsider_token, _ = _create_user_token("eval-outsider")
    member_headers = {"Authorization": f"Bearer {member_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    with TestClient(app, headers=admin_headers) as client:
        add_member = client.put(
            f"/api/v1/projects/{project.id}/members/{member_id}",
            json={"role": "member"},
        )
        assert add_member.status_code == 200, add_member.text

        # Non-member cannot read or write
        assert (
            client.get(
                "/api/v1/eval-questions",
                params={"project": project.id},
                headers=outsider_headers,
            ).status_code
            == 404
        )

        # Member cannot create/update/delete
        member_create = client.post(
            "/api/v1/eval-questions",
            params={"project": project.id},
            headers=member_headers,
            json={
                "question": "昨天订单数是多少",
                "caliber": "统计昨日支付成功订单数",
                "reference_sql": "SELECT COUNT(*) FROM shop.orders WHERE dt = '2026-01-01'",
            },
        )
        assert member_create.status_code == 403

        # Admin creates a confirmed holdout item, a confirmed dev item, and an unconfirmed draft
        holdout_res = client.post(
            "/api/v1/eval-questions",
            params={"project": project.id},
            json={
                "id": "q-holdout",
                "question": "保留集题目：近7日GMV",
                "caliber": "只统计已支付订单金额之和",
                "reference_sql": "SELECT SUM(amount) FROM shop.orders WHERE status = 'paid'",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["shop.orders.amount", "shop.orders.status"],
                "split": "holdout",
                "answer_confirmed": True,
                "category": "核心指标",
            },
        )
        assert holdout_res.status_code == 201, holdout_res.text

        dev_res = client.post(
            "/api/v1/eval-questions",
            params={"project": project.id},
            json={
                "id": "q-dev",
                "question": "开发集题目：昨日订单量",
                "caliber": "统计昨日所有订单数量",
                "reference_sql": "SELECT COUNT(id) FROM shop.orders WHERE dt = '2026-01-01'",
                "split": "dev",
                "answer_confirmed": True,
                "category": "单表聚合",
            },
        )
        assert dev_res.status_code == 201, dev_res.text
        assert dev_res.json()["expected_tables"] == ["shop.orders"]
        assert "shop.orders.id" in dev_res.json()["expected_fields"]
        assert dev_res.json()["created_at"].endswith("Z")
        assert dev_res.json()["updated_at"].endswith("Z")

        draft_res = client.post(
            "/api/v1/eval-questions",
            params={"project": project.id},
            json={
                "id": "q-draft",
                "question": "待确认题目：退款金额",
                "caliber": "统计退款成功金额",
                "reference_sql": "SELECT SUM(refund_amount) FROM shop.refunds",
                "answer_confirmed": False,
            },
        )
        assert draft_res.status_code == 201, draft_res.text
        assert draft_res.json()["split"] == "dev"
        assert draft_res.json()["answer_confirmed"] is False

        # Verify file location is isolated outside source_directory
        eval_file = project_context.eval_questions_path(project.id)
        source_dir = project_context.project_source_directory(project.id)
        assert eval_file.exists()
        assert not eval_file.resolve().is_relative_to(source_dir.resolve())
        source_files = client.get(
            "/api/v1/source/files", params={"project": project.id}
        )
        assert source_files.status_code == 200
        assert not any("questions.yaml" in str(item) for item in source_files.json())
        git_status = subprocess.check_output(
            ["git", "-C", str(source_dir), "status", "--porcelain"],
            text=True,
        )
        assert "questions.yaml" not in git_status

        # Validate compatibility with eval.load_questions
        loaded = eval_mod.load_questions(eval_file)
        assert [q.id for q in loaded] == ["q-holdout", "q-dev", "q-draft"]

        # Admin sees full details for all items
        admin_list = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        )
        assert admin_list.status_code == 200
        by_id_admin = {item["id"]: item for item in admin_list.json()}
        assert by_id_admin["q-holdout"]["reference_sql"] is not None
        assert by_id_admin["q-holdout"]["redacted"] is False
        assert by_id_admin["q-draft"]["reference_sql"] is not None

        # Member sees dev confirmed details, but holdout reference_sql & draft details are hidden
        member_list = client.get(
            "/api/v1/eval-questions",
            params={"project": project.id},
            headers=member_headers,
        )
        assert member_list.status_code == 200
        by_id_member = {item["id"]: item for item in member_list.json()}
        assert by_id_member["q-dev"]["redacted"] is False
        assert by_id_member["q-dev"]["reference_sql"] is not None
        assert by_id_member["q-holdout"]["redacted"] is True
        assert by_id_member["q-holdout"]["reference_sql"] is None
        assert by_id_member["q-holdout"]["caliber"] is None
        assert by_id_member["q-holdout"]["expected_tables"] == []
        assert by_id_member["q-holdout"]["expected_fields"] == []
        assert by_id_member["q-draft"]["redacted"] is True
        assert by_id_member["q-draft"]["reference_sql"] is None
        assert by_id_member["q-draft"]["caliber"] is None

        # Member cannot PATCH or DELETE
        assert (
            client.patch(
                "/api/v1/eval-questions/q-draft",
                params={"project": project.id},
                headers=member_headers,
                json={"answer_confirmed": True},
            ).status_code
            == 403
        )
        assert (
            client.delete(
                "/api/v1/eval-questions/q-draft",
                params={"project": project.id},
                headers=member_headers,
            ).status_code
            == 403
        )

        # Admin confirms q-draft
        patch_res = client.patch(
            "/api/v1/eval-questions/q-draft",
            params={"project": project.id},
            json={"answer_confirmed": True, "notes": "已复核"},
        )
        assert patch_res.status_code == 200, patch_res.text
        assert patch_res.json()["answer_confirmed"] is True
        assert patch_res.json()["notes"] == "已复核"

        # Existing holdout stays holdout after confirmation of new item
        after_admin = {
            item["id"]: item
            for item in client.get(
                "/api/v1/eval-questions", params={"project": project.id}
            ).json()
        }
        assert after_admin["q-holdout"]["split"] == "holdout"

        # Delete item
        del_res = client.delete(
            "/api/v1/eval-questions/q-draft", params={"project": project.id}
        )
        assert del_res.status_code == 204
        assert (
            client.delete(
                "/api/v1/eval-questions/q-draft", params={"project": project.id}
            ).status_code
            == 404
        )

        # Verify spark & doris dialects are accepted and unsupported dialects rejected
        for dialect_name in ("spark", "doris"):
            d_res = client.post(
                "/api/v1/eval-questions",
                params={"project": project.id},
                json={
                    "id": f"q-{dialect_name}",
                    "question": f"{dialect_name} 方言订单统计",
                    "caliber": "统计订单总数",
                    "reference_sql": "SELECT COUNT(*) FROM shop.orders",
                    "dialect": dialect_name,
                    "answer_confirmed": True,
                },
            )
            assert d_res.status_code == 201, d_res.text
            assert d_res.json()["dialect"] == dialect_name
            assert (
                client.delete(
                    f"/api/v1/eval-questions/q-{dialect_name}",
                    params={"project": project.id},
                ).status_code
                == 204
            )
        assert (
            client.post(
                "/api/v1/eval-questions",
                params={"project": project.id},
                json={
                    "question": "unsupported dialect",
                    "caliber": "c",
                    "reference_sql": "SELECT COUNT(*) FROM shop.orders",
                    "dialect": "mysql",
                },
            ).status_code
            == 422
        )

        # Archive project -> 409 on GET and mutations
        archive_res = client.post(f"/api/v1/projects/{project.id}/archive")
        assert archive_res.status_code == 200
        assert (
            client.get(
                "/api/v1/eval-questions", params={"project": project.id}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/v1/eval-questions",
                params={"project": project.id},
                json={
                    "question": "q",
                    "caliber": "c",
                    "reference_sql": "SELECT COUNT(*) FROM shop.orders",
                },
            ).status_code
            == 409
        )


def test_stratified_split_and_project_deletion_cleanup(tmp_path: Path, monkeypatch):
    from semantica_dataagent import eval as eval_mod
    from semantica_dataagent import project_context
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Split Project", "warehouse")

    with TestClient(app, headers=headers) as client:
        for index in range(10):
            category = "分类A" if index < 5 else "分类B"
            res = client.post(
                "/api/v1/eval-questions",
                params={"project": project.id},
                json={
                    "id": f"q-{index}",
                    "question": f"问题 {index}",
                    "caliber": f"口径 {index}",
                    "reference_sql": f"SELECT COUNT(*) FROM shop.orders_{index}",
                    "category": category,
                    "answer_confirmed": True,
                },
            )
            assert res.status_code == 201, res.text

        items = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        holdout_items = [item for item in items if item["split"] == "holdout"]
        dev_items = [item for item in items if item["split"] == "dev"]
        assert len(dev_items) == 7
        assert len(holdout_items) == 3
        # Stratified across both categories
        assert {item["category"] for item in holdout_items} == {"分类A", "分类B"}
        assert "shop.orders_0.*" in items[0]["expected_fields"]
        assert all(
            eval_mod.sql_satisfies_caliber(q, q.reference_sql)
            for q in eval_mod.load_questions(
                project_context.eval_questions_path(project.id)
            )
        )

        eval_dir = project_context.project_eval_directory(project.id)
        assert eval_dir.exists()

        # Archive & delete project -> eval directory removed
        from semantica_dataagent import project_deletion

        monkeypatch.setattr(project_deletion, "_delete_neo4j", lambda *_a, **_kw: None)
        monkeypatch.setattr(project_deletion, "_delete_milvus", lambda *_a, **_kw: None)
        assert client.post(f"/api/v1/projects/{project.id}/archive").status_code == 200
        del_proj = client.post(
            f"/api/v1/projects/{project.id}/delete",
            json={"name": "Split Project"},
        )
        assert del_proj.status_code == 204, del_proj.text
        assert not eval_dir.exists()


def test_recover_projects_does_not_create_default_source_when_eval_exists(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent import project_context

    _configure(tmp_path, monkeypatch)
    external_source = tmp_path / "custom-default-source"
    external_source.mkdir(parents=True)
    monkeypatch.setenv("DATAAGENT_SOURCE_DIR", str(external_source))

    eval_path = project_context.eval_questions_path("default")
    eval_path.parent.mkdir(parents=True, exist_ok=True)
    eval_path.write_text("questions: []\n", encoding="utf-8")

    project_context.recover_projects()
    assert not (project_context.projects_root() / "default" / "source").exists()


def _seed_conversation_turn(
    project_id: str,
    owner_id: str,
    *,
    question: str = "近7日订单总额是多少",
    answer: str = "近7日订单总额为 1000。",
    sql_blocks: list[dict] | None = None,
    with_validate_sql_pass: bool = False,
) -> tuple[str, int]:
    import json
    import uuid
    from datetime import UTC, datetime

    from semantica_dataagent.db import Conversation, Turn, TurnEvent, sessions

    now = datetime.now(UTC)
    conv_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            Conversation(
                id=conv_id,
                project_id=project_id,
                owner_id=owner_id,
                title=question[:30],
                agent="pi",
                provider_id="p1",
                model="m1",
                created_at=now,
                updated_at=now,
            )
        )
        turn = Turn(
            conversation_id=conv_id,
            provider_id="p1",
            model="m1",
            effort="medium",
            sequence=1,
            question=question,
            status="done",
            answer=answer,
            sql=json.dumps(sql_blocks or [], ensure_ascii=False),
            citations="[]",
            started_at=now,
            finished_at=now,
        )
        db.add(turn)
        db.flush()
        turn_id = turn.id
        if with_validate_sql_pass:
            db.add(
                TurnEvent(
                    turn_id=turn_id,
                    seq=1,
                    type="tool_end",
                    data=json.dumps(
                        {
                            "name": "mcp__semantica__validate_sql",
                            "is_error": False,
                            "summary": "status=pass",
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return conv_id, turn_id


def test_turn_feedback_accurate_requires_ownership_scope_and_validated_sql(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent import eval as eval_mod
    from semantica_dataagent import project_context, sql_validation
    from semantica_dataagent.app import app
    from semantica_dataagent.db import Token, digest, sessions
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    with sessions()() as db:
        admin_id = db.get(Token, digest(admin_token)).id
    project = create_project_record("Feedback Warehouse", "warehouse")

    member_token, member_id = _create_user_token("fb-member")
    outsider_token, _ = _create_user_token("fb-outsider")
    member_headers = {"Authorization": f"Bearer {member_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    with TestClient(app, headers=admin_headers) as client:
        assert (
            client.put(
                f"/api/v1/projects/{project.id}/members/{member_id}",
                json={"role": "member"},
            ).status_code
            == 200
        )

        # Turn without validate_sql (even if SQL block present without validation) -> 422
        conv_unval, turn_unval = _seed_conversation_turn(
            project.id,
            member_id,
            sql_blocks=[{"sql": "SELECT COUNT(*) FROM shop.orders", "dialect": "hive"}],
            with_validate_sql_pass=False,
        )
        detail_unval = client.get(
            f"/api/v1/conversations/{conv_unval}", headers=member_headers
        )
        assert detail_unval.status_code == 200
        assert detail_unval.json()["turns"][0]["has_validated_sql"] is False

        bad_fb = client.post(
            f"/api/v1/conversations/{conv_unval}/turns/{turn_unval}/feedback",
            headers=member_headers,
            json={"rating": "accurate"},
        )
        assert bad_fb.status_code == 422

        # Confirmed reference_sql failing validate_sql -> 422
        monkeypatch.setattr(
            sql_validation,
            "validate_sql",
            lambda sql, dialect="hive", project_id=None: {
                "status": "fail",
                "issues": [{"message": "unknown table"}],
            },
        )
        bad_ref = client.post(
            f"/api/v1/conversations/{conv_unval}/turns/{turn_unval}/feedback",
            headers=member_headers,
            json={
                "rating": "accurate",
                "reference_sql": "SELECT COUNT(*) FROM shop.missing_tbl",
            },
        )
        assert bad_ref.status_code == 422

        # Turn with validate_sql pass -> member writes answer_confirmed=False draft
        conv_val, turn_val = _seed_conversation_turn(
            project.id,
            member_id,
            question="昨日成交订单数是多少",
            sql_blocks=[
                {
                    "sql": "SELECT COUNT(order_id) FROM shop.orders WHERE dt = '2026-01-01'",
                    "dialect": "hive",
                }
            ],
            with_validate_sql_pass=True,
        )

        # Non-owner (even outsider or another user) gets 404
        assert (
            client.post(
                f"/api/v1/conversations/{conv_val}/turns/{turn_val}/feedback",
                headers=outsider_headers,
                json={"rating": "accurate"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/conversations/{conv_val}/turns/{turn_val}/feedback",
                headers=admin_headers,
                json={"rating": "accurate"},
            ).status_code
            == 404
        )

        # Member submits accurate feedback -> creates unconfirmed draft
        member_fb = client.post(
            f"/api/v1/conversations/{conv_val}/turns/{turn_val}/feedback",
            headers=member_headers,
            json={"rating": "accurate", "category": "订单指标"},
        )
        assert member_fb.status_code == 201, member_fb.text
        fb_body = member_fb.json()
        assert fb_body["answer_confirmed"] is False
        assert fb_body["split"] == "dev"
        assert fb_body["eval_question"]["redacted"] is True
        assert fb_body["eval_question"]["reference_sql"] is None

        # Turn detail reflects accurate feedback
        conv_detail = client.get(
            f"/api/v1/conversations/{conv_val}", headers=member_headers
        ).json()
        assert conv_detail["turns"][0]["has_validated_sql"] is True
        assert conv_detail["turns"][0]["feedback_accurate"] is True
        assert conv_detail["turns"][0]["eval_answer_confirmed"] is False
        assert conv_detail["turns"][0]["eval_question_id"] == fb_body["question_id"]

        # Repeat feedback is idempotent (still 1 item)
        repeat_fb = client.post(
            f"/api/v1/conversations/{conv_val}/turns/{turn_val}/feedback",
            headers=member_headers,
            json={"rating": "accurate"},
        )
        assert repeat_fb.status_code in (200, 201)
        assert (
            len(
                client.get(
                    "/api/v1/eval-questions", params={"project": project.id}
                ).json()
            )
            == 1
        )

        # Admin turn accurate feedback creates confirmed item with stratified split
        for i in range(4):
            a_conv, a_turn = _seed_conversation_turn(
                project.id,
                admin_id,
                question=f"管理员问题 {i}",
                sql_blocks=[
                    {"sql": f"SELECT COUNT(*) FROM shop.tbl_{i}", "dialect": "hive"}
                ],
                with_validate_sql_pass=True,
            )
            a_res = client.post(
                f"/api/v1/conversations/{a_conv}/turns/{a_turn}/feedback",
                json={"rating": "accurate", "category": "订单指标"},
            )
            assert a_res.status_code == 201, a_res.text
            assert a_res.json()["answer_confirmed"] is True

        all_questions = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        confirmed_splits = [q["split"] for q in all_questions if q["answer_confirmed"]]
        assert "holdout" in confirmed_splits and "dev" in confirmed_splits
        assert (
            len(
                eval_mod.load_questions(project_context.eval_questions_path(project.id))
            )
            == 5
        )

        # Archived project -> 409 on feedback
        assert client.post(f"/api/v1/projects/{project.id}/archive").status_code == 200
        assert (
            client.post(
                f"/api/v1/conversations/{conv_val}/turns/{turn_val}/feedback",
                headers=member_headers,
                json={"rating": "accurate"},
            ).status_code
            == 409
        )


def test_approved_correction_promotes_validated_sql_to_eval_questions(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent import eval as eval_mod
    from semantica_dataagent import knowledge, project_context, sql_validation
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Promote Project", "document")
    member_token, member_id = _create_user_token("promo-member")
    member_headers = {"Authorization": f"Bearer {member_token}"}

    def fake_execute(_store, _query, parameters):
        src_dir = project_context.project_source_directory(project.id)
        doc_path = src_dir / "knowledge" / "metrics" / "gmv.md"
        content = (
            doc_path.read_text(encoding="utf-8")
            if doc_path.exists()
            else "# 旧口径\n\n未包含退款过滤\n"
        )
        return [
            {
                "node": {
                    "content": content,
                    "source_file": "knowledge/metrics/gmv.md",
                }
            }
        ]

    monkeypatch.setattr(
        knowledge, "existing_refs", lambda refs, project_id=None: set(refs)
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(knowledge, "execute", fake_execute)
    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="hive", project_id=None: (
            {"status": "fail", "issues": [{"message": "bad table"}]}
            if "invalid_tbl" in sql
            else {"status": "pass", "issues": []}
        ),
    )

    with TestClient(app, headers=admin_headers) as client:
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
        import base64

        assert (
            client.post(
                "/api/v1/source/upload",
                params={"project": project.id},
                json={
                    "target": "knowledge/metrics",
                    "files": [
                        {
                            "path": "gmv.md",
                            "content_base64": base64.b64encode(
                                "# 旧口径\n\n未包含退款过滤\n".encode()
                            ).decode(),
                        }
                    ],
                },
            ).status_code
            == 200
        )

        # Member marks turn accurate (unconfirmed draft), then proposes correction tied to conversation
        conv_id, turn_id = _seed_conversation_turn(
            project.id,
            member_id,
            question="GMV 口径是否包含退款",
            sql_blocks=[
                {
                    "sql": "SELECT SUM(amount) FROM shop.orders WHERE status = 'paid'",
                    "dialect": "hive",
                }
            ],
            with_validate_sql_pass=True,
        )
        fb_res = client.post(
            f"/api/v1/conversations/{conv_id}/turns/{turn_id}/feedback",
            headers=member_headers,
            json={"rating": "accurate"},
        )
        assert fb_res.status_code == 201
        draft_qid = fb_res.json()["question_id"]

        prop1 = client.post(
            "/api/v1/corrections",
            params={"project": project.id},
            headers=member_headers,
            json={
                "target": "document:gmv",
                "target_kind": "document",
                "patch": "# GMV 口径\n\n仅统计已支付订单金额\n",
                "reason": "补充退款剔除说明",
                "conversation_id": conv_id,
                "turn_id": turn_id,
            },
        )
        assert prop1.status_code == 201, prop1.text
        prop1_id = prop1.json()["id"]

        # While pending, draft remains unconfirmed
        before_qs = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        assert before_qs[0]["answer_confirmed"] is False

        # Approving promotes existing draft in place -> answer_confirmed=True without duplicate
        app1 = client.post(
            f"/api/v1/corrections/{prop1_id}/approve",
            params={"project": project.id},
        )
        assert app1.status_code == 200, app1.text
        after_qs = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        assert len(after_qs) == 1
        assert after_qs[0]["id"] == draft_qid
        assert after_qs[0]["answer_confirmed"] is True
        assert after_qs[0]["proposal_id"] == prop1_id

        # Direct correction with validated reference_sql appends new confirmed eval question on approve
        prop2 = client.post(
            "/api/v1/corrections",
            params={"project": project.id},
            headers=member_headers,
            json={
                "target": "document:gmv",
                "target_kind": "document",
                "patch": "# GMV 口径 v2\n\n按支付日期统计已支付金额\n",
                "reason": "明确按支付日期统计",
                "question": "昨天支付GMV是多少",
                "caliber": "按支付时间统计已支付订单金额",
                "reference_sql": "SELECT SUM(pay_amount) FROM shop.orders WHERE status = 'paid'",
                "category": "核心指标",
            },
        )
        assert prop2.status_code == 201, prop2.text
        app2 = client.post(
            f"/api/v1/corrections/{prop2.json()['id']}/approve",
            params={"project": project.id},
        )
        assert app2.status_code == 200, app2.text
        all_qs = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        assert len(all_qs) == 2
        promoted = next(
            item for item in all_qs if item["proposal_id"] == prop2.json()["id"]
        )
        assert promoted["answer_confirmed"] is True
        assert promoted["source"] == "correction"

        # Invalid SQL on correction does NOT promote on approval
        prop3 = client.post(
            "/api/v1/corrections",
            params={"project": project.id},
            headers=member_headers,
            json={
                "target": "document:gmv",
                "target_kind": "document",
                "patch": "# GMV 口径 v3\n\n备注更新\n",
                "reason": "文档微调",
                "question": "坏SQL不应该进入题集",
                "reference_sql": "SELECT COUNT(*) FROM shop.invalid_tbl",
            },
        )
        assert prop3.status_code == 201, prop3.text
        app3 = client.post(
            f"/api/v1/corrections/{prop3.json()['id']}/approve",
            params={"project": project.id},
        )
        assert app3.status_code == 200, app3.text
        final_qs = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        assert len(final_qs) == 2
        assert (
            len(
                eval_mod.load_questions(project_context.eval_questions_path(project.id))
            )
            == 2
        )


def test_auto_applied_member_correction_does_not_auto_confirm_eval_question(
    tmp_path: Path, monkeypatch
):
    import base64

    from semantica_dataagent import knowledge, project_context, sql_validation
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Auto Promo Project", "document")
    member_token, member_id = _create_user_token("auto-promo-member")
    member_headers = {"Authorization": f"Bearer {member_token}"}

    def fake_execute(_store, _query, _parameters):
        src_dir = project_context.project_source_directory(project.id)
        doc_path = src_dir / "knowledge" / "metrics" / "gmv.md"
        content = (
            doc_path.read_text(encoding="utf-8") if doc_path.exists() else "# 旧口径\n"
        )
        return [
            {
                "node": {
                    "content": content,
                    "source_file": "knowledge/metrics/gmv.md",
                }
            }
        ]

    monkeypatch.setattr(
        knowledge, "existing_refs", lambda refs, project_id=None: set(refs)
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(knowledge, "execute", fake_execute)
    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="hive", project_id=None: {"status": "pass", "issues": []},
    )

    with TestClient(app, headers=admin_headers) as client:
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
        assert (
            client.post(
                "/api/v1/source/upload",
                params={"project": project.id},
                json={
                    "target": "knowledge/metrics",
                    "files": [
                        {
                            "path": "gmv.md",
                            "content_base64": base64.b64encode(
                                "# 旧口径\n".encode()
                            ).decode(),
                        }
                    ],
                },
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/api/v1/projects/{project.id}/evolution-policy",
                json={"correction_require_review": False},
            ).status_code
            == 200
        )

        # Member auto_applied correction MUST NOT become answer_confirmed=True
        mem_prop = client.post(
            "/api/v1/corrections",
            params={"project": project.id},
            headers=member_headers,
            json={
                "target": "document:gmv",
                "target_kind": "document",
                "patch": "# 成员直通修改\n",
                "reason": "成员自动生效纠错",
                "question": "成员自动生效的问题是否会直接进回归集",
                "caliber": "成员口径",
                "reference_sql": "SELECT COUNT(*) FROM shop.orders",
            },
        )
        assert mem_prop.status_code == 201, mem_prop.text
        assert mem_prop.json()["status"] == "auto_applied"

        qs_after_member = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        assert all(q["answer_confirmed"] is False for q in qs_after_member)

        # Admin-authored auto_applied correction DOES promote as answer_confirmed=True
        adm_prop = client.post(
            "/api/v1/corrections",
            params={"project": project.id},
            json={
                "target": "document:gmv",
                "target_kind": "document",
                "patch": "# 管理员直通修改\n",
                "reason": "管理员自动生效纠错",
                "question": "管理员自动生效的问题应该直接确认",
                "caliber": "管理员口径",
                "reference_sql": "SELECT SUM(amount) FROM shop.orders",
            },
        )
        assert adm_prop.status_code == 201, adm_prop.text
        assert adm_prop.json()["status"] == "auto_applied"

        qs_after_admin = client.get(
            "/api/v1/eval-questions", params={"project": project.id}
        ).json()
        adm_q = next(
            q for q in qs_after_admin if q["proposal_id"] == adm_prop.json()["id"]
        )
        assert adm_q["answer_confirmed"] is True


def test_eval_questions_500_scrubs_internal_yaml_parse_errors(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent import project_context
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    headers = {"Authorization": f"Bearer {admin_token}"}
    project = create_project_record("Corrupt Eval Proj", "warehouse")

    q_path = project_context.eval_questions_path(project.id)
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(
        "questions: [unclosed sk-secret-leak-999999999999", encoding="utf-8"
    )

    with TestClient(app, headers=headers) as client:
        for method, path in (
            ("get", "/api/v1/eval-questions"),
            ("patch", "/api/v1/eval-questions/q1"),
            ("delete", "/api/v1/eval-questions/q1"),
        ):
            kwargs = {"params": {"project": project.id}}
            if method == "patch":
                kwargs["json"] = {"notes": "x"}
            resp = getattr(client, method)(path, **kwargs)
            assert resp.status_code == 500
            detail = resp.json()["detail"]
            assert "sk-secret-leak" not in detail
            assert "invalid question YAML" not in detail


def test_revoke_turn_feedback_removes_draft_and_respects_confirmations(
    tmp_path: Path, monkeypatch
):
    from semantica_dataagent import knowledge, sql_validation
    from semantica_dataagent.app import app
    from semantica_dataagent.db import Token, digest, sessions
    from semantica_dataagent.project_api import create_project_record

    admin_token = _configure(tmp_path, monkeypatch)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    with sessions()() as db:
        admin_id = db.get(Token, digest(admin_token)).id
    project = create_project_record("Revoke Feedback Project", "warehouse")

    member_token, member_id = _create_user_token("revoke-member")
    outsider_token, _ = _create_user_token("revoke-outsider")
    member_headers = {"Authorization": f"Bearer {member_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    monkeypatch.setattr(
        sql_validation,
        "validate_sql",
        lambda sql, dialect="hive", project_id=None: {"status": "pass", "issues": []},
    )
    monkeypatch.setattr(
        knowledge, "existing_refs", lambda refs, project_id=None: set(refs)
    )

    with TestClient(app, headers=admin_headers) as client:
        assert (
            client.put(
                f"/api/v1/projects/{project.id}/members/{member_id}",
                json={"role": "member"},
            ).status_code
            == 200
        )

        # 1. Member marks turn accurate -> DELETE removes draft and resets turn flags
        conv_m, turn_m = _seed_conversation_turn(
            project.id,
            member_id,
            question="昨日活跃用户数是多少",
            sql_blocks=[
                {
                    "sql": "SELECT COUNT(DISTINCT user_id) FROM shop.orders",
                    "dialect": "hive",
                }
            ],
            with_validate_sql_pass=True,
        )
        post_res = client.post(
            f"/api/v1/conversations/{conv_m}/turns/{turn_m}/feedback",
            headers=member_headers,
            json={"rating": "accurate"},
        )
        assert post_res.status_code == 201, post_res.text

        # Non-owner DELETE -> 404
        assert (
            client.delete(
                f"/api/v1/conversations/{conv_m}/turns/{turn_m}/feedback",
                headers=outsider_headers,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/v1/conversations/{conv_m}/turns/{turn_m}/feedback",
                headers=admin_headers,
            ).status_code
            == 404
        )

        del_res = client.delete(
            f"/api/v1/conversations/{conv_m}/turns/{turn_m}/feedback",
            headers=member_headers,
        )
        assert del_res.status_code == 204
        # Repeat DELETE is idempotent
        assert (
            client.delete(
                f"/api/v1/conversations/{conv_m}/turns/{turn_m}/feedback",
                headers=member_headers,
            ).status_code
            == 204
        )
        conv_detail = client.get(
            f"/api/v1/conversations/{conv_m}", headers=member_headers
        ).json()
        assert conv_detail["turns"][0]["feedback_accurate"] is False
        assert conv_detail["turns"][0]["eval_question_id"] is None
        assert conv_detail["turns"][0]["eval_answer_confirmed"] is None
        assert (
            client.get("/api/v1/eval-questions", params={"project": project.id}).json()
            == []
        )

        # 2. Member marks accurate, admin confirms via PATCH -> member DELETE blocked (409)
        repost_res = client.post(
            f"/api/v1/conversations/{conv_m}/turns/{turn_m}/feedback",
            headers=member_headers,
            json={"rating": "accurate"},
        )
        assert repost_res.status_code == 201
        confirmed_qid = repost_res.json()["question_id"]
        assert (
            client.patch(
                f"/api/v1/eval-questions/{confirmed_qid}",
                params={"project": project.id},
                json={"answer_confirmed": True},
            ).status_code
            == 200
        )
        blocked_del = client.delete(
            f"/api/v1/conversations/{conv_m}/turns/{turn_m}/feedback",
            headers=member_headers,
        )
        assert blocked_del.status_code == 409
        assert (
            len(
                client.get(
                    "/api/v1/eval-questions", params={"project": project.id}
                ).json()
            )
            == 1
        )

        # 3. Admin marks own turn accurate (answer_confirmed=True) -> admin DELETE succeeds
        conv_a, turn_a = _seed_conversation_turn(
            project.id,
            admin_id,
            question="昨日客单价是多少",
            sql_blocks=[
                {"sql": "SELECT AVG(pay_amount) FROM shop.orders", "dialect": "hive"}
            ],
            with_validate_sql_pass=True,
        )
        adm_post = client.post(
            f"/api/v1/conversations/{conv_a}/turns/{turn_a}/feedback",
            json={"rating": "accurate"},
        )
        assert adm_post.status_code == 201
        assert adm_post.json()["answer_confirmed"] is True

        assert (
            client.delete(
                f"/api/v1/conversations/{conv_a}/turns/{turn_a}/feedback",
            ).status_code
            == 204
        )
        adm_detail = client.get(f"/api/v1/conversations/{conv_a}").json()
        assert adm_detail["turns"][0]["feedback_accurate"] is False
        assert adm_detail["turns"][0]["eval_question_id"] is None

        # 4. Archived project -> 409 on DELETE
        assert client.post(f"/api/v1/projects/{project.id}/archive").status_code == 200
        assert (
            client.delete(
                f"/api/v1/conversations/{conv_a}/turns/{turn_a}/feedback",
            ).status_code
            == 409
        )
