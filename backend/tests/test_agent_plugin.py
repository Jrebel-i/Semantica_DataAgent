"""Contract tests for the agent runner plugin interface and shared helpers."""

import asyncio
import json
import sys
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar

import pytest

from semantica_dataagent.agent_plugin import (
    EVENT_DETAIL_LIMIT,
    EventParser,
    SandboxTurn,
    agent_process,
    done_event,
    error_event,
    has_session_file,
    mcp_headers,
    mcp_server_config,
    read_marker,
    redact_event,
    request_secrets,
    result_summary,
    text_event,
    tool_start_event,
    write_json,
)
from semantica_dataagent.agent_runner import discover_runners
from semantica_dataagent.agent_types import (
    DONE,
    ERROR,
    TEXT_DELTA,
    AgentTurnRequest,
    ProviderConfig,
)
from semantica_dataagent.plugins import AGENT_RUNNER_GROUP


class EchoRunner:
    """A deliberately tiny runner used only by tests and the how-to document."""

    agent_id = "echo"

    def available(self):
        return True

    async def run(self, request):
        parser = EventParser()
        for event in parser.emit_text(f"echo: {request.prompt}"):
            yield event
        yield parser.finish()


@dataclass
class FakeEntryPoint:
    name: str
    factory: object

    def load(self):
        return self.factory


def _request(workspace: Path) -> AgentTurnRequest:
    return AgentTurnRequest(
        conversation_id="conversation-one",
        prompt="hello",
        session_id="conversation-one",
        workspace=workspace,
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
        turn_id=7,
    )


def test_builtin_agents_are_registered_entry_point_plugins():
    found = discover_runners(entry_points(group=AGENT_RUNNER_GROUP))
    assert {"pi", "claude", "codex"} <= set(found)
    assert {agent_id: runner.agent_id for agent_id, runner in found.items()} == {
        agent_id: agent_id for agent_id in found
    }


def test_discovery_loads_a_test_only_echo_plugin():
    found = discover_runners([FakeEntryPoint("echo", EchoRunner)])
    assert set(found) == {"echo"}
    assert found["echo"].agent_id == "echo"


def test_discovery_rejects_duplicate_agent_ids():
    with pytest.raises(ValueError, match="Duplicate agent runner"):
        discover_runners(
            [
                FakeEntryPoint("echo", EchoRunner),
                FakeEntryPoint("echo", EchoRunner),
            ]
        )


def test_discovery_rejects_a_plugin_that_does_not_implement_the_interface():
    class NotARunner:
        agent_id = "broken"

    with pytest.raises(TypeError, match="available"):
        discover_runners([FakeEntryPoint("broken", NotARunner)])


def test_fake_echo_agent_produces_the_unified_event_stream(tmp_path):
    async def collect():
        return [event async for event in EchoRunner().run(_request(tmp_path))]

    events = asyncio.run(collect())
    assert [event.type for event in events] == [TEXT_DELTA, DONE]
    assert events[0].data == {"text": "echo: hello"}
    assert events[1].data == {
        "text": "echo: hello",
        "stop_reason": None,
        "sql": [],
        "citations": [],
    }


def test_done_event_is_the_single_place_that_builds_sql_and_citations():
    event = done_event(
        "用 shop.orders。\n```sql\nSELECT 1\n```\n引用 table:shop.orders", "stop"
    )
    assert event.type == DONE
    assert event.data == {
        "text": "用 shop.orders。\n```sql\nSELECT 1\n```\n引用 table:shop.orders",
        "stop_reason": "stop",
        "sql": [{"sql": "SELECT 1", "dialect": "hive"}],
        "citations": [{"ref": "table:shop.orders", "type": "table"}],
    }
    assert error_event("boom").type == ERROR


def test_event_parser_accumulates_text_and_reports_errors_by_default():
    parser = EventParser()
    assert [event.type for event in parser.emit_text("hel")] == [TEXT_DELTA]
    assert [event.type for event in parser.emit_text("lo")] == [TEXT_DELTA]
    assert parser.finish().data["text"] == "hello"

    parser.error = "provider failed"
    assert parser.finish() == error_event("provider failed")


def test_mcp_configuration_uses_shared_headers_without_secrets(tmp_path):
    request = _request(tmp_path)
    assert mcp_headers(request) == {
        "Authorization": "Bearer ${DATAAGENT_TOKEN}",
        "x-dataagent-conversation-id": "conversation-one",
        "x-dataagent-turn-id": "7",
    }
    server = mcp_server_config(request)
    assert server["url"] == "http://127.0.0.1:8000/mcp/"
    assert "${DATAAGENT_TOKEN}" in json.dumps(server)
    assert "user-token-secret-value" not in json.dumps(server)
    assert request_secrets(request) == (
        "user-token-secret-value",
        "provider-secret-value",
    )


def test_redaction_covers_nested_event_data():
    event = text_event("token-value key-value")
    redacted = redact_event(event, ("token-value", "key-value"))
    assert redacted.data == {"text": "[redacted] [redacted]"}


def test_tool_start_records_truncated_input_and_redacts_secrets():
    sql = "SELECT " + "x" * 3000
    event = tool_start_event(
        "call-1",
        "mcp__dataagent__validate_sql",
        {"sql": sql, "dialect": "hive", "api_key": "secret-value"},
    )

    assert event.data["summary"].startswith("dialect=hive")
    assert '"sql": "SELECT x' in event.data["input"]
    assert len(event.data["input"]) <= 2000
    redacted = redact_event(event, ("secret-value",))
    assert "secret-value" not in json.dumps(redacted.data, ensure_ascii=False)


def test_validate_tool_end_summary_includes_issue_details():
    summary = result_summary(
        {
            "structuredContent": {
                "status": "issues",
                "issues": [
                    {
                        "kind": "partition",
                        "table": "shop.orders",
                        "field": "dt",
                        "message": "分区表 shop.orders 缺少分区条件: dt",
                        "suggestion": "WHERE dt = (SELECT MAX(dt) FROM shop.orders)",
                    }
                ],
            }
        },
        name="mcp__dataagent__validate_sql",
    )

    assert summary.startswith("status=issues")
    assert "缺少分区条件" in summary
    assert "MAX(dt)" in summary


def test_propose_tool_end_summary_includes_error_code_and_message():
    summary = result_summary(
        {
            "structuredContent": {
                "code": "invalid_input",
                "message": "SQL validation did not pass: 缺少分区条件",
                "retryable": False,
            }
        },
        name="mcp__dataagent__propose_query",
    )

    assert "invalid_input" in summary
    assert "缺少分区条件" in summary


FIXTURES = Path(__file__).parent / "fixtures" / "issue_165"


def _real_runner_tool_ends(agent: str):
    if agent == "pi":
        from semantica_dataagent.pi_runner import PiEventParser

        parser = PiEventParser()
    elif agent == "claude":
        from semantica_dataagent.claude_runner import ClaudeEventParser

        parser = ClaudeEventParser()
    else:
        from semantica_dataagent.codex_runner import CodexEventParser

        parser = CodexEventParser()
    events = []
    for line in (FIXTURES / f"{agent}.jsonl").read_text().splitlines():
        events.extend(parser.feed(json.loads(line)))
    return events


@pytest.mark.parametrize("agent", ["pi", "claude", "codex"])
def test_real_runner_validate_sql_event_summary_keeps_status_and_issues(agent):
    ends = [
        event
        for event in _real_runner_tool_ends(agent)
        if event.type == "tool_end" and event.data["name"].endswith("validate_sql")
    ]
    assert len(ends) == 1
    summary = ends[0].data["summary"]
    assert "status=issues" in summary
    assert "缺少分区条件" in summary
    assert "MAX(dt)" in summary
    assert "unknown" not in summary


@pytest.mark.parametrize("agent", ["pi", "claude", "codex"])
def test_real_runner_propose_query_event_summary_keeps_error_code(agent):
    ends = [
        event
        for event in _real_runner_tool_ends(agent)
        if event.type == "tool_end" and event.data["name"].endswith("propose_query")
    ]
    assert len(ends) == 1
    summary = ends[0].data["summary"]
    assert "invalid_input" in summary
    assert "SQL validation did not pass" in summary


def test_unparseable_validate_sql_result_falls_back_to_truncated_raw_text():
    truncated = (
        '{"status": "issues", "issues": [{"kind": "partition", '
        '"message": "分区表 shop.orders 缺少分区条件: dt'
    )
    summary = result_summary(
        {"content": [{"type": "text", "text": truncated}]},
        name="mcp__dataagent__validate_sql",
    )

    assert "unknown" not in summary
    assert truncated in summary


def test_long_unparseable_result_is_truncated_to_the_event_limit():
    raw = "x" * (EVENT_DETAIL_LIMIT + 500)
    summary = result_summary(
        {"content": [{"type": "text", "text": raw}]},
        name="mcp__dataagent__validate_sql",
    )

    assert "unknown" not in summary
    assert len(summary) <= EVENT_DETAIL_LIMIT
    assert summary.startswith("x")


def test_sandbox_turn_registers_starts_and_always_closes(tmp_path):
    from semantica_dataagent import agent_sandbox

    class FakeSandbox:
        instances: ClassVar[list] = []

        def __init__(self, workspace, model_url, mcp_url):
            self.workspace = workspace
            self.model_url = "http://relay/model"
            self.mcp_url = "http://relay/mcp"
            self.name = "fake-sandbox"
            self.started = False
            self.closed = False
            FakeSandbox.instances.append(self)

        def start(self):
            self.started = True

        def close(self):
            self.closed = True

        def wrap_command(self, command, env, keys):
            return ["kubectl", *command], b"{}\n"

    async def scenario():
        turn = SandboxTurn(_request(tmp_path), sandbox_class=FakeSandbox)
        async with turn:
            sandbox = turn.sandbox
            assert sandbox.started
            assert agent_sandbox.ACTIVE["conversation-one"] is sandbox
            command, header = turn.wrap_command(["tool"], {"A": "1"}, ("A",))
            assert command == ["kubectl", "tool"]
            assert header == b"{}\n"
        return sandbox

    sandbox = asyncio.run(scenario())
    assert sandbox.closed
    assert "conversation-one" not in agent_sandbox.ACTIVE
    assert FakeSandbox.instances[-1] is sandbox


def test_sandbox_turn_unregisters_when_start_fails(tmp_path):
    from semantica_dataagent import agent_sandbox

    class BrokenSandbox:
        def __init__(self, workspace, model_url, mcp_url):
            self.model_url = model_url
            self.mcp_url = mcp_url
            self.closed = False

        def start(self):
            raise RuntimeError("cannot start")

        def close(self):
            self.closed = True

    sandbox = None

    async def scenario():
        nonlocal sandbox
        turn = SandboxTurn(_request(tmp_path), sandbox_class=BrokenSandbox)
        with pytest.raises(RuntimeError, match="cannot start"):
            async with turn:
                pass
        sandbox = turn.sandbox

    asyncio.run(scenario())
    assert sandbox.closed
    assert "conversation-one" not in agent_sandbox.ACTIVE


def test_agent_process_streams_json_lines_and_reports_exit(tmp_path):
    script = (
        "import json, sys\n"
        "print(json.dumps({'type': 'one'}), flush=True)\n"
        "print('oops', file=sys.stderr, flush=True)\n"
        "raise SystemExit(3)\n"
    )

    async def scenario():
        async with agent_process(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
        ) as process:
            lines = [json.loads(line) async for line in process.lines()]
            code = await process.wait()
            stderr = process.stderr_text()
        return lines, code, stderr

    lines, code, stderr = asyncio.run(scenario())
    assert lines == [{"type": "one"}]
    assert code == 3
    assert stderr.strip() == "oops"


def test_agent_process_can_send_a_stdin_header_and_keep_stdin_open(tmp_path):
    script = (
        "import json, sys\n"
        "header = json.loads(sys.stdin.readline())\n"
        "prompt = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'header': header, 'prompt': prompt}), flush=True)\n"
    )

    async def scenario():
        async with agent_process(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            header=b'{"mode": "k8s"}\n',
            stdin_open=True,
        ) as process:
            process.write(b'{"prompt": "hello"}\n')
            lines = [json.loads(line) async for line in process.lines()]
            await process.wait()
        return lines

    assert asyncio.run(scenario()) == [
        {"header": {"mode": "k8s"}, "prompt": {"prompt": "hello"}}
    ]


def test_session_resume_helpers_read_and_write_markers(tmp_path):
    marker = tmp_path / "nested" / "thread-id.json"
    write_json(marker, {"thread_id": "thread-one"})
    assert read_marker(marker, "thread_id") == "thread-one"
    assert marker.stat().st_mode & 0o777 == 0o600

    sessions = tmp_path / "sessions"
    assert has_session_file(sessions, "session-one") is False
    path = sessions / "projects" / "workspace" / "session-one.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    assert has_session_file(sessions, "session-one") is True
