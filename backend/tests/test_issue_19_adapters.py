import importlib
import json
from pathlib import Path

import pytest
from semantica_dataagent.agent_runner import get_runner


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_cli_sample_is_translated_to_unified_events(agent):
    runner = get_runner(agent)
    assert runner is not None, f"{agent} is not registered"
    module = importlib.import_module(f"semantica_dataagent.{agent}_runner")
    parser = getattr(module, f"{agent.title()}EventParser")()
    path = Path(__file__).parent / "fixtures" / "issue_19" / f"{agent}.jsonl"
    events = []
    for line in path.read_text().splitlines():
        events.extend(parser.feed(json.loads(line)))
    events.append(parser.finish())
    assert any(
        event.type == "text_delta" and event.data["text"] == "hello" for event in events
    )
    assert events[-1].type == "done"
    assert events[-1].data["text"] == "hello"


def test_claude_stream_emits_text_once():
    from semantica_dataagent.claude_runner import ClaudeEventParser

    parser = ClaudeEventParser()
    path = Path(__file__).parent / "fixtures" / "issue_19" / "claude-stream.jsonl"
    events = [
        event
        for line in path.read_text().splitlines()
        for event in parser.feed(json.loads(line))
    ]
    assert (
        "".join(event.data["text"] for event in events if event.type == "text_delta")
        == "hello"
    )
    assert parser.finish().data["text"] == "hello"


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_mcp_configuration_passes_the_conversation_header(agent, tmp_path):
    import tomllib

    from semantica_dataagent.agent_runner import AgentTurnRequest, ProviderConfig

    request = AgentTurnRequest(
        conversation_id="conversation-one",
        prompt="question",
        session_id="conversation-one",
        workspace=tmp_path / agent,
        provider=ProviderConfig(
            id="provider-one",
            name="Local",
            type="anthropic-messages",
            base_url="http://127.0.0.1:3425",
            models=["model"],
            api_key="provider-secret-value",
        ),
        model="model",
        mcp_url="http://127.0.0.1:8000/mcp/",
        mcp_token="user-token-secret-value",
    )
    config = get_runner(agent).write_config(request)

    if agent == "claude":
        path = config / "mcp.json"
        text = path.read_text()
        headers = json.loads(text)["mcpServers"]["dataagent"]["headers"]
        assert "${DATAAGENT_TOKEN}" in text
    else:
        path = config / "config.toml"
        text = path.read_text()
        headers = tomllib.loads(text)["mcp_servers"]["dataagent"]["http_headers"]
        assert tomllib.loads(text)["mcp_servers"]["dataagent"][
            "bearer_token_env_var"
        ] == "DATAAGENT_TOKEN"

    assert headers["x-dataagent-conversation-id"] == "conversation-one"
    assert "user-token-secret-value" not in text
    assert "provider-secret-value" not in text
