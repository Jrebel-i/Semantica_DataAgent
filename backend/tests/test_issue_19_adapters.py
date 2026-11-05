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
