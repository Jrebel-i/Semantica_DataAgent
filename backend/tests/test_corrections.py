"""Acceptance checks for correction proposals and knowledge gaps."""

from test_clarification import _requester, call, client  # noqa: F401


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
