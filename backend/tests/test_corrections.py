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
