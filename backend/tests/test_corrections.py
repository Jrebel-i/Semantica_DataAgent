"""Acceptance checks for correction proposals and knowledge gaps."""

from test_clarification import _requester, call

pytest_plugins = ["test_clarification"]


def test_knowledge_gap_is_recorded_for_the_requester(client):
    from semantica_dataagent.app import create_mcp

    c, token = client
    requester = _requester(token="gap-reporter")
    result = call(
        create_mcp(),
        "report_knowledge_gap",
        {"question": "销售额如何计算？", "missing": "支付口径定义"},
        requester,
    )
    assert not result.isError, result.structuredContent
    response = c.get(
        "/api/v1/knowledge-gaps", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    assert any(
        item["question"] == "销售额如何计算？" and item["missing"] == "支付口径定义"
        for item in response.json()
    )


def test_user_cannot_approve_correction(client):
    c, _ = client
    requester = _requester(role="user", token="correction-reader")
    response = c.post(
        "/api/v1/corrections/nonexistent/approve",
        headers={"Authorization": f"Bearer {requester.token}"},
    )
    assert response.status_code == 403


def test_proposal_rejects_target_absent_from_current_generation(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set())
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/missing.md",
            "patch": "修正后的定义\n",
            "reason": "定义有误",
        },
        _requester(token="missing-target"),
    )
    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert result.structuredContent["message"] != "Unknown tool"


def test_correction_and_gap_tools_publish_self_contained_schemas():
    import json

    from semantica_dataagent.app import create_mcp

    tools = {
        tool.name: tool.parameters for tool in create_mcp()._tool_manager.list_tools()
    }
    for name in ("propose_correction", "report_knowledge_gap"):
        assert name in tools, f"{name} is unavailable"
        schema = json.dumps(tools[name])
        assert '"$ref"' not in schema
        assert '"$defs"' not in schema


def test_user_cannot_reject_correction(client):
    c, _ = client
    requester = _requester(role="user", token="rejection-reader")
    response = c.post(
        "/api/v1/corrections/nonexistent/reject",
        headers={"Authorization": f"Bearer {requester.token}"},
    )
    assert response.status_code == 403


def test_proposal_rejects_another_users_conversation(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.db import Conversation, sessions

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    with sessions().begin() as db:
        db.add(
            Conversation(
                id="private-conversation",
                owner_id="someone-else",
                title="销售额",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/sales.md",
            "patch": "修正后的定义\n",
            "reason": "定义有误",
        },
        _requester(token="different-owner"),
        "private-conversation",
    )
    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert result.structuredContent["message"] != "Unknown tool"


def test_rejected_document_proposal_does_not_change_source(
    client, monkeypatch, tmp_path
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    document = root / "knowledge" / "metrics" / "sales.md"
    document.parent.mkdir(parents=True)
    document.write_text("旧的销售额定义\n")
    configured = c.put(
        "/api/v1/settings",
        headers=headers,
        json={"values": {"source_directory": str(root)}},
    )
    assert configured.status_code == 200, configured.text
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [
            {
                "node": {
                    "content": document.read_text(),
                    "source_file": "knowledge/metrics/sales.md",
                }
            }
        ],
    )
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/sales.md",
            "patch": "新的销售额定义\n",
            "reason": "口径已更新",
        },
        _requester(token="reject-proposer"),
    )
    assert not result.isError, result.structuredContent
    proposal_id = result.structuredContent["proposal_id"]
    details = c.get(f"/api/v1/corrections/{proposal_id}", headers=headers)
    assert details.status_code == 200, details.text
    assert "-旧的销售额定义" in details.json()["diff"]
    assert "+新的销售额定义" in details.json()["diff"]
    response = c.post(f"/api/v1/corrections/{proposal_id}/reject", headers=headers)
    assert response.status_code == 200, response.text
    assert document.read_text() == "旧的销售额定义\n"
    assert (
        c.get(f"/api/v1/corrections/{proposal_id}", headers=headers).json()["status"]
        == "rejected"
    )


def test_approval_commits_document_and_enqueues_incremental_build(
    client, monkeypatch, tmp_path
):
    import subprocess

    from test_source_api import upload

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"source_directory": str(root)}},
        ).status_code
        == 200
    )
    assert (
        upload(c, headers, "knowledge/metrics", {"sales.md": "旧定义\n"}).status_code
        == 200
    )
    before = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [
            {
                "node": {
                    "content": "旧定义\n",
                    "source_file": "knowledge/metrics/sales.md",
                }
            }
        ],
    )
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/sales.md",
            "patch": "新定义\n",
            "reason": "口径已更新",
        },
        _requester(token="approval-proposer"),
    )
    assert not result.isError, result.structuredContent
    proposal_id = result.structuredContent["proposal_id"]
    response = c.post(f"/api/v1/corrections/{proposal_id}/approve", headers=headers)
    assert response.status_code == 200, response.text
    after = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    assert before != after
    assert (root / "knowledge/metrics/sales.md").read_text() == "新定义\n"
    message = subprocess.check_output(
        ["git", "-C", str(root), "log", "-1", "--format=%B"], text=True
    )
    assert proposal_id in message
    builds = c.get("/api/v1/builds", headers=headers).json()
    assert any(build["build_type"] == "incremental" for build in builds)


def test_metadata_override_survives_reimport_of_same_source(client, tmp_path):
    import json

    from semantica_dataagent.canonical import CanonicalAdapter
    from semantica_dataagent.db import MetadataOverride, sessions
    from semantica_dataagent.table_stage import apply_overrides

    source = tmp_path / "warehouse"
    source.mkdir()
    (source / "fields.json").write_text(
        json.dumps(
            [
                {
                    "database": "shop",
                    "table": "orders",
                    "name": "amount",
                    "type": "decimal",
                    "comment": "导出注释",
                    "partition": False,
                }
            ]
        )
    )
    with sessions().begin() as db:
        db.add(
            MetadataOverride(target="field:shop.orders.amount", comment="人工修正注释")
        )
    for _ in range(2):
        imported = CanonicalAdapter().read(source, {}).records
        corrected = apply_overrides(imported)
        assert corrected[0].data["comment"] == "人工修正注释"
        assert imported[0].data["comment"] == "导出注释"


def test_failed_atomic_replace_preserves_original_document(tmp_path, monkeypatch):
    from semantica_dataagent import corrections

    path = tmp_path / "document.md"
    path.write_text("original\n")

    def fail_replace(*_args):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(corrections.os, "replace", fail_replace)
    import pytest

    with pytest.raises(OSError, match="simulated rename failure"):
        corrections._atomic_write(path, "changed\n")
    assert path.read_text() == "original\n"
    assert list(tmp_path.iterdir()) == [path]
