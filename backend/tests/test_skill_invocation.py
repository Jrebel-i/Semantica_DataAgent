"""Tests for explicit Skill invocation, ephemeral staging, multi-runner parity, and secret redaction (#323)."""

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_skill_catalog import archive

from semantica_dataagent import agent_runner, telemetry
from semantica_dataagent.agent_sandbox import SessionSandbox
from semantica_dataagent.agent_types import (
    DONE,
    ERROR,
    AgentTurnRequest,
    ProviderConfig,
)
from semantica_dataagent.claude_runner import ClaudeRunner
from semantica_dataagent.codex_runner import CodexRunner
from semantica_dataagent.db import QueryExecution, Turn, sessions
from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox
from semantica_dataagent.pi_runner import PiRunner
from semantica_dataagent.session_runtime import LocalSessionRuntime
from semantica_dataagent.skill_catalog import SkillSnapshot

pytest_plugins = ("test_skill_catalog",)


def _setup_provider(client: TestClient, token: str) -> str:
    resp = client.post(
        "/api/v1/providers",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "LocalTest",
            "type": "openai-completions",
            "base_url": "http://127.0.0.1:4321",
            "api_key": "provider-key-secret",
            "models": ["model-a"],
        },
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _upload_and_enable(
    client: TestClient,
    token: str,
    files: dict[str, bytes | str],
    name: str = "sample",
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    pkg = archive(files)
    assert (
        client.post("/api/v1/skills", headers=headers, content=pkg).status_code == 201
    )
    assert (
        client.put(
            f"/api/v1/skills/{name}/enabled", headers=headers, json={"enabled": True}
        ).status_code
        == 200
    )


def _mock_script(tmp_path: Path, agent: str, capture_dir: Path) -> Path:
    capture_dir.mkdir(parents=True, exist_ok=True)
    script = tmp_path / f"mock-{agent}.py"
    if agent == "pi":
        body = f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
capture = Path({str(capture_dir)!r})
req = json.loads(sys.stdin.readline())
skill_path = None
for idx, arg in enumerate(sys.argv[1:]):
    if arg == "--skill" and idx + 1 < len(sys.argv[1:]):
        skill_path = sys.argv[1:][idx + 1]
staged_text = ""
if skill_path:
    staged_text = (Path(skill_path) / "SKILL.md").read_text(encoding="utf-8")
(capture / "record.json").write_text(json.dumps({{
    "argv": sys.argv[1:],
    "prompt": req["message"],
    "env": {{k: v for k, v in os.environ.items() if k in ("JIRA_TOKEN", "DATAAGENT_TOKEN")}},
    "staged_text": staged_text,
}}), encoding="utf-8")
secret_leak = os.environ.get("JIRA_TOKEN", "")
print(json.dumps({{"type": "message_end", "message": {{
    "role": "assistant",
    "content": [{{"type": "text", "text": f"pi-ok {{secret_leak}}"}}],
    "stopReason": "stop",
}}}}), flush=True)
print(json.dumps({{"type": "agent_settled"}}), flush=True)
"""
    elif agent == "claude":
        body = f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
capture = Path({str(capture_dir)!r})
add_dir = None
for idx, arg in enumerate(sys.argv[1:]):
    if arg == "--add-dir" and idx + 1 < len(sys.argv[1:]):
        add_dir = sys.argv[1:][idx + 1]
staged_text = ""
if add_dir:
    for path in Path(add_dir).rglob("SKILL.md"):
        staged_text = path.read_text(encoding="utf-8")
prompt = sys.argv[-1]
if "--input-format" in sys.argv:
    payload = json.loads(sys.stdin.readline())
    prompt = payload["message"]["content"][0]["text"]
(capture / "record.json").write_text(json.dumps({{
    "argv": sys.argv[1:],
    "prompt": prompt,
    "env": {{k: v for k, v in os.environ.items() if k in ("JIRA_TOKEN", "DATAAGENT_TOKEN")}},
    "staged_text": staged_text,
}}), encoding="utf-8")
secret_leak = os.environ.get("JIRA_TOKEN", "")
print(json.dumps({{"type": "result", "is_error": False, "result": f"claude-ok {{secret_leak}}"}}), flush=True)
"""
    else:
        body = f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
capture = Path({str(capture_dir)!r})
prompt = sys.argv[-1]
(capture / "record.json").write_text(json.dumps({{
    "argv": sys.argv[1:],
    "prompt": prompt,
    "env": {{k: v for k, v in os.environ.items() if k in ("JIRA_TOKEN", "DATAAGENT_TOKEN")}},
    "config": (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text(encoding="utf-8"),
}}), encoding="utf-8")
secret_leak = os.environ.get("JIRA_TOKEN", "")
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": f"codex-ok {{secret_leak}}"}}}}), flush=True)
"""
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_agents_endpoint_exposes_supports_skills(client):
    c, token = client
    response = c.get("/api/v1/agents", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert by_id["pi"]["supports_skills"] is True
    assert by_id["claude"]["supports_skills"] is True
    assert by_id["codex"]["supports_skills"] is True


def test_available_skills_reports_caller_secret_readiness(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    _upload_and_enable(
        c,
        token,
        {
            "jira-helper/SKILL.md": (
                "---\n"
                "name: jira-helper\n"
                "description: Jira helper\n"
                "secrets: [JIRA_TOKEN, JIRA_USER]\n"
                "---\nInstructions\n"
            )
        },
        name="jira-helper",
    )
    first = c.get("/api/v1/skills/available?project=default", headers=headers).json()
    jira = next(item for item in first if item["name"] == "jira-helper")
    assert jira["configured_secrets"] == []
    assert jira["missing_secrets"] == ["JIRA_TOKEN", "JIRA_USER"]
    assert jira["ready"] is False

    assert (
        c.put(
            "/api/v1/skills/jira-helper/secrets/JIRA_TOKEN?project=default",
            headers=headers,
            json={"value": "tok-1"},
        ).status_code
        == 204
    )
    second = c.get("/api/v1/skills/available?project=default", headers=headers).json()
    jira2 = next(item for item in second if item["name"] == "jira-helper")
    assert jira2["configured_secrets"] == ["JIRA_TOKEN"]
    assert jira2["missing_secrets"] == ["JIRA_USER"]
    assert jira2["ready"] is False

    assert (
        c.put(
            "/api/v1/skills/jira-helper/secrets/JIRA_USER?project=default",
            headers=headers,
            json={"value": "alice"},
        ).status_code
        == 204
    )
    third = c.get("/api/v1/skills/available?project=default", headers=headers).json()
    jira3 = next(item for item in third if item["name"] == "jira-helper")
    assert jira3["configured_secrets"] == ["JIRA_TOKEN", "JIRA_USER"]
    assert jira3["missing_secrets"] == []
    assert jira3["ready"] is True
    assert "tok-1" not in json.dumps(third)


@pytest.mark.parametrize("agent_id", ["pi", "claude", "codex"])
def test_ask_with_uploaded_skill_stages_ephemerally_injects_secrets_and_redacts_telemetry(
    client, tmp_path, monkeypatch, agent_id
):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider_id = _setup_provider(c, token)
    secret_value = "synthetic-jira-super-secret-xyz"
    _upload_and_enable(
        c,
        token,
        {
            "jira-helper/SKILL.md": (
                "---\n"
                "name: jira-helper\n"
                "description: Jira helper\n"
                "secrets: [JIRA_TOKEN]\n"
                "---\nUNTRUSTED SKILL BODY MARKER\n"
            ),
            "jira-helper/scripts/helper.py": "print('helper')\n",
        },
        name="jira-helper",
    )
    assert (
        c.put(
            "/api/v1/skills/jira-helper/secrets/JIRA_TOKEN?project=default",
            headers=headers,
            json={"value": secret_value},
        ).status_code
        == 204
    )

    capture_dir = tmp_path / f"cap-{agent_id}"
    script = _mock_script(tmp_path, agent_id, capture_dir)
    if agent_id == "pi":
        runner = PiRunner(binary=str(script))
    elif agent_id == "claude":
        runner = ClaudeRunner(binary=str(script))
    else:
        runner = CodexRunner(binary=str(script))
    monkeypatch.setitem(agent_runner.RUNNERS, agent_id, runner)

    telemetry_events = []
    monkeypatch.setattr(
        telemetry,
        "submit",
        lambda kind, event, secrets=(): telemetry_events.append(
            (kind, event, tuple(secrets))
        ),
    )

    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "Check my Jira ticket",
            "agent": agent_id,
            "provider_id": provider_id,
            "model": "model-a",
            "project": "default",
            "skill_name": "jira-helper",
        },
    )
    assert response.status_code == 200
    assert secret_value not in response.text
    assert "[redacted]" in response.text

    record = json.loads((capture_dir / "record.json").read_text(encoding="utf-8"))
    assert record["env"]["JIRA_TOKEN"] == secret_value
    assert "UNTRUSTED SKILL BODY MARKER" not in " ".join(record["argv"])
    if agent_id == "codex":
        assert "UNTRUSTED SKILL BODY MARKER" not in record["config"]
    else:
        assert "UNTRUSTED SKILL BODY MARKER" in record["staged_text"]
    assert "jira-helper/SKILL.md" in record["prompt"]
    assert "JIRA_TOKEN" in record["prompt"]
    assert secret_value not in record["prompt"]

    # Telemetry receives the skill secret for scrubbing and records only skill_name metadata.
    assert telemetry_events
    for _, evt, secrets in telemetry_events:
        assert secret_value in secrets
        assert evt["metadata"].get("skill_name") == "jira-helper"
        assert secret_value not in json.dumps(evt)

    # Conversation detail exposes skill_name on the Turn and no leaked secret or staged files.
    conv_id = next(
        line.split("data: ", 1)[1]
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"conversation_id"' in line
    )
    cid = json.loads(conv_id)["conversation_id"]
    detail = c.get(f"/api/v1/conversations/{cid}", headers=headers).json()
    assert detail["turns"][0]["skill_name"] == "jira-helper"
    assert secret_value not in json.dumps(detail)

    workspace = tmp_path / "agent-workspaces" / cid
    # Skill files must never land in persistent workspace or sessions directories.
    assert not list(workspace.rglob("helper.py"))


def test_pi_runner_stderr_failure_redacts_skill_secrets(tmp_path):
    script = tmp_path / "failing-pi.py"
    script.write_text(
        """#!/usr/bin/env python3
import os, sys
sys.stderr.write("fatal: token=" + os.environ.get("JIRA_TOKEN", "") + "\\n")
sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    secret_val = "pi-stderr-secret-999"
    req = AgentTurnRequest(
        conversation_id="conv-pi-err",
        prompt="hi",
        session_id="conv-pi-err",
        workspace=tmp_path / "ws",
        provider=ProviderConfig(
            id="p1",
            name="Local",
            type="openai-completions",
            base_url="http://127.0.0.1:4000",
            models=["m1"],
        ),
        model="m1",
        mcp_url="http://127.0.0.1:8000/mcp/",
        mcp_token="mcp-secret",
        skill=SkillSnapshot(
            "s1", "upload", ("JIRA_TOKEN",), (("s1/SKILL.md", b"---"),), None
        ),
        skill_secrets={"JIRA_TOKEN": secret_val},
    )

    import asyncio

    async def collect():
        return [ev async for ev in PiRunner(binary=str(script)).run(req)]

    events = asyncio.run(collect())
    assert events[-1].type == ERROR
    assert secret_val not in events[-1].data["message"]
    assert "[redacted]" in events[-1].data["message"]


def test_local_session_runtime_cleans_up_ephemeral_stage_dir(tmp_path):
    rt = LocalSessionRuntime(
        tmp_path, "http://127.0.0.1:4000", "http://127.0.0.1:8000/mcp/"
    )
    stage_dir = Path(rt.stage_ephemeral_files((("demo/SKILL.md", b"hello"),)))
    assert (stage_dir / "demo" / "SKILL.md").read_bytes() == b"hello"
    rt.close()
    assert not stage_dir.exists()


@pytest.mark.parametrize("mode", ["docker", "k8s"])
def test_container_sandboxes_stage_to_tmp_only(tmp_path, monkeypatch, mode):
    captured = {}

    def fake_run(cmd, *, input=None, capture_output=False, check=False, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if mode == "docker":
        sb = SessionSandbox(
            tmp_path, "http://127.0.0.1:4000", "http://127.0.0.1:8000/mcp/"
        )
        stage_dir = sb.stage_ephemeral_files(
            (("demo/SKILL.md", b"ok"), ("demo/a.txt", b"123"))
        )
        assert captured["cmd"][:4] == ["docker", "exec", "-i", sb.name]
    else:
        monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path))
        ws = tmp_path / "conv1"
        ws.mkdir()
        sb = KubernetesSessionSandbox(
            ws, "http://127.0.0.1:4000", "http://127.0.0.1:8000/mcp/"
        )
        stage_dir = sb.stage_ephemeral_files(
            (("demo/SKILL.md", b"ok"), ("demo/a.txt", b"123"))
        )
        assert "kubectl" in captured["cmd"][0]
        assert sb.name in captured["cmd"]

    assert stage_dir.startswith("/tmp/sda-skill-")
    assert "/workspace" not in stage_dir
    assert "/sessions" not in stage_dir
    with tarfile.open(fileobj=io.BytesIO(captured["input"]), mode="r:") as tf:
        names = {m.name: m.type for m in tf.getmembers()}
        assert names["demo/SKILL.md"] == tarfile.REGTYPE
        assert names["demo/a.txt"] == tarfile.REGTYPE


def test_unsupported_runner_and_missing_secret_and_auth_failures_reject_before_turn(
    client, tmp_path, monkeypatch
):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider_id = _setup_provider(c, token)
    _upload_and_enable(
        c,
        token,
        {
            "needs-key/SKILL.md": (
                "---\nname: needs-key\ndescription: Needs key\nsecrets: [API_KEY]\n---\nBody\n"
            )
        },
        name="needs-key",
    )

    # Unsupported runner -> 422
    class NoSkillRunner:
        agent_id = "noskill"
        effort_levels = ()
        supports_skills = False

        def available(self):
            return True

        async def run(self, request):
            yield

    monkeypatch.setitem(agent_runner.RUNNERS, "noskill", NoSkillRunner())
    r_unsupported = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "hi",
            "agent": "noskill",
            "provider_id": provider_id,
            "model": "model-a",
            "skill_name": "dataagent-retrieval",
        },
    )
    assert r_unsupported.status_code == 422

    # Missing personal secret -> 409
    monkeypatch.setitem(
        agent_runner.RUNNERS,
        "pi",
        PiRunner(binary=str(_mock_script(tmp_path, "pi", tmp_path / "c1"))),
    )
    r_missing = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "hi",
            "agent": "pi",
            "provider_id": provider_id,
            "model": "model-a",
            "skill_name": "needs-key",
        },
    )
    assert r_missing.status_code == 409

    # Disabled skill -> 404
    assert (
        c.put(
            "/api/v1/skills/needs-key/enabled", headers=headers, json={"enabled": False}
        ).status_code
        == 200
    )
    r_disabled = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "hi",
            "agent": "pi",
            "provider_id": provider_id,
            "model": "model-a",
            "skill_name": "needs-key",
        },
    )
    assert r_disabled.status_code == 404

    # Verify zero orphan turns were created by rejected requests.
    with sessions()() as db:
        assert db.query(Turn).count() == 0


def test_ordinary_slash_text_builtin_skill_attachments_and_document_project_type(
    client, tmp_path, monkeypatch
):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider_id = _setup_provider(c, token)
    doc_project = c.post(
        "/api/v1/projects", headers=headers, json={"name": "Docs", "type": "document"}
    ).json()["id"]

    recorded_requests: list[AgentTurnRequest] = []

    class CaptureRunner:
        agent_id = "pi"
        effort_levels = ("low",)
        supports_skills = True

        def available(self):
            return True

        async def run(self, request):
            from semantica_dataagent.agent_types import AgentEvent

            recorded_requests.append(request)
            yield AgentEvent(DONE, {"text": "ok", "sql": [], "citations": []})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CaptureRunner())

    # 1. Ordinary slash text without skill_name proceeds cleanly without selecting a skill
    r_plain = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "Check metrics for a/b and /not-a-skill",
            "agent": "pi",
            "provider_id": provider_id,
            "model": "model-a",
            "project": doc_project,
        },
    )
    assert r_plain.status_code == 200
    assert recorded_requests[-1].skill is None
    assert recorded_requests[-1].project_type == "document"

    # 2. Explicit builtin document retrieval skill + attachment multipart form
    r_builtin = c.post(
        "/api/v1/conversations/ask-with-attachments",
        headers=headers,
        data={
            "question": "Read attached note",
            "agent": "pi",
            "provider_id": provider_id,
            "model": "model-a",
            "project": doc_project,
            "skill_name": "dataagent-document-retrieval",
        },
        files={"attachments": ("note.txt", b"hello attachment", "text/plain")},
    )
    assert r_builtin.status_code == 200
    assert recorded_requests[-1].skill is not None
    assert recorded_requests[-1].skill.name == "dataagent-document-retrieval"
    assert recorded_requests[-1].skill.source == "builtin"
    assert recorded_requests[-1].project_type == "document"


def test_confirm_query_does_not_reinject_skill_or_secrets(client, monkeypatch):
    from semantica_dataagent import query_api

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider_id = _setup_provider(c, token)
    secret_value = "one-turn-only-secret"
    _upload_and_enable(
        c,
        token,
        {
            "secret-skill/SKILL.md": (
                "---\nname: secret-skill\ndescription: Secret\nsecrets: [API_KEY]\n---\nBody\n"
            )
        },
        name="secret-skill",
    )
    assert (
        c.put(
            "/api/v1/skills/secret-skill/secrets/API_KEY?project=default",
            headers=headers,
            json={"value": secret_value},
        ).status_code
        == 204
    )

    seen_requests: list[AgentTurnRequest] = []
    seen_secrets_during_run: list[dict[str, str]] = []

    class QueryCaptureRunner:
        agent_id = "pi"
        effort_levels = ()
        supports_skills = True

        def available(self):
            return True

        async def run(self, request):
            from semantica_dataagent.agent_types import QUERY_PLAN, AgentEvent

            seen_requests.append(request)
            seen_secrets_during_run.append(dict(request.skill_secrets))
            if len(seen_requests) == 1:
                yield AgentEvent(
                    QUERY_PLAN,
                    {
                        "id": "plan-1",
                        "sql": "SELECT 1",
                        "data_source": "dtb",
                        "tables": ["t"],
                        "estimated_partitions": 1,
                        "estimated_rows": 1,
                        "row_limit": 100,
                    },
                )
            else:
                yield AgentEvent(DONE, {"text": "done", "sql": [], "citations": []})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", QueryCaptureRunner())

    r1 = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "run query",
            "agent": "pi",
            "provider_id": provider_id,
            "model": "model-a",
            "skill_name": "secret-skill",
        },
    )
    assert r1.status_code == 200
    assert seen_secrets_during_run[0] == {"API_KEY": secret_value}
    assert seen_requests[0].skill_secrets == {}

    # Seed query plan record cleanly and mock execute_for_turn to confirm query resumption.
    with sessions().begin() as db:
        from semantica_dataagent.db import Conversation

        turn = db.query(Turn).one()
        conv_id = turn.conversation_id
        turn_id = turn.id
        conv = db.get(Conversation, conv_id)
        db.add(
            QueryExecution(
                id="plan-1",
                conversation_id=conv_id,
                turn_id=turn_id,
                owner_id=conv.owner_id,
                sql="SELECT 1",
                status="executed",
                rows=json.dumps({"columns": ["x"], "data": [[1]]}),
            )
        )

    async def fake_execute(query_id, requester, cid, tid):
        return {"id": query_id, "columns": ["x"], "rows": [[1]], "row_count": 1}

    monkeypatch.setattr(query_api, "execute_for_turn", fake_execute)
    monkeypatch.setattr(
        query_api,
        "_call_config",
        lambda _pid: (
            "dtb",
            {"url": "http://127.0.0.1:9999", "interpret_results": True},
        ),
    )

    r2 = c.post(
        f"/api/v1/conversations/{conv_id}/turns/{turn_id}/queries/plan-1/execute",
        headers=headers,
    )
    assert r2.status_code == 200
    assert len(seen_requests) == 2
    assert seen_requests[1].skill is None
    assert seen_secrets_during_run[1] == {}


@pytest.mark.parametrize("agent_id", ["pi", "claude", "codex"])
def test_enabled_project_skills_mount_by_default_without_slash_hint(
    client, tmp_path, monkeypatch, agent_id
):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider_id = _setup_provider(c, token)
    secret_value = "ambient-jira-secret-456"

    _upload_and_enable(
        c,
        token,
        {
            "alpha-helper/SKILL.md": (
                "---\n"
                "name: alpha-helper\n"
                "description: Ready ambient helper\n"
                "secrets: [JIRA_TOKEN]\n"
                "---\nALPHA BODY MARKER\n"
            ),
            "alpha-helper/scripts/run.py": "print('alpha')\n",
        },
        name="alpha-helper",
    )
    assert (
        c.put(
            "/api/v1/skills/alpha-helper/secrets/JIRA_TOKEN?project=default",
            headers=headers,
            json={"value": secret_value},
        ).status_code
        == 204
    )

    # Another enabled skill whose secret is NOT configured must still mount files
    # without blocking normal chat turns with 409.
    _upload_and_enable(
        c,
        token,
        {
            "beta-unconfigured/SKILL.md": (
                "---\n"
                "name: beta-unconfigured\n"
                "description: Ambient skill without secret yet\n"
                "secrets: [MISSING_KEY]\n"
                "---\nBETA BODY MARKER\n"
            ),
            "beta-unconfigured/scripts/tool.py": "print('beta')\n",
        },
        name="beta-unconfigured",
    )

    # Disabled skill must NOT be mounted.
    assert (
        c.post(
            "/api/v1/skills",
            headers=headers,
            content=archive(
                {
                    "gamma-disabled/SKILL.md": (
                        "---\n"
                        "name: gamma-disabled\n"
                        "description: Disabled skill\n"
                        "---\nGAMMA BODY\n"
                    )
                }
            ),
        ).status_code
        == 201
    )

    capture_dir = tmp_path / f"cap-ambient-{agent_id}"
    script = _mock_script(tmp_path, agent_id, capture_dir)
    if agent_id == "pi":
        runner = PiRunner(binary=str(script))
    elif agent_id == "claude":
        runner = ClaudeRunner(binary=str(script))
    else:
        runner = CodexRunner(binary=str(script))
    monkeypatch.setitem(agent_runner.RUNNERS, agent_id, runner)

    response = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "Continue checking the query without re-selecting /skill",
            "agent": agent_id,
            "provider_id": provider_id,
            "model": "model-a",
            "project": "default",
        },
    )
    assert response.status_code == 200
    assert secret_value not in response.text
    assert "[redacted]" in response.text

    record = json.loads((capture_dir / "record.json").read_text(encoding="utf-8"))
    assert record["env"]["JIRA_TOKEN"] == secret_value
    assert "alpha-helper/SKILL.md" in record["prompt"]
    assert "beta-unconfigured/SKILL.md" in record["prompt"]
    assert "gamma-disabled" not in record["prompt"]
    assert "JIRA_TOKEN" in record["prompt"]
    assert "MISSING_KEY" not in record["prompt"]
    assert secret_value not in record["prompt"]
    if agent_id == "pi":
        skill_args = [
            record["argv"][i + 1]
            for i, arg in enumerate(record["argv"])
            if arg == "--skill"
        ]
        assert any(arg.endswith("/alpha-helper") for arg in skill_args)
        assert any(arg.endswith("/beta-unconfigured") for arg in skill_args)
        assert not any("gamma-disabled" in arg for arg in skill_args)
    elif agent_id == "claude":
        assert "BETA BODY MARKER" in record["staged_text"] or (
            "--add-dir" in record["argv"]
        )

    conv_id = next(
        line.split("data: ", 1)[1]
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"conversation_id"' in line
    )
    cid = json.loads(conv_id)["conversation_id"]
    detail = c.get(f"/api/v1/conversations/{cid}", headers=headers).json()
    assert detail["turns"][0]["skill_name"] is None


def test_multiturn_followup_without_slash_keeps_mounted_skills(
    client, tmp_path, monkeypatch
):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    provider_id = _setup_provider(c, token)
    _upload_and_enable(
        c,
        token,
        {
            "dtb-demo/SKILL.md": (
                "---\n"
                "name: dtb-demo\n"
                "description: DTB query skill\n"
                "secrets: [GATEWAY_KEY]\n"
                "---\nDTB BODY\n"
            ),
            "dtb-demo/scripts/run.py": "print('run')\n",
        },
        name="dtb-demo",
    )
    assert (
        c.put(
            "/api/v1/skills/dtb-demo/secrets/GATEWAY_KEY?project=default",
            headers=headers,
            json={"value": "gw-secret-val"},
        ).status_code
        == 204
    )

    seen: list[AgentTurnRequest] = []

    class CaptureTurnRunner:
        agent_id = "pi"
        effort_levels = ()
        supports_skills = True

        def available(self):
            return True

        async def run(self, request):
            from semantica_dataagent.agent_types import AgentEvent

            seen.append(request)
            yield AgentEvent(DONE, {"text": "ok", "sql": [], "citations": []})

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", CaptureTurnRunner())

    first = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "question": "Run with hint",
            "agent": "pi",
            "provider_id": provider_id,
            "model": "model-a",
            "project": "default",
            "skill_name": "dtb-demo",
        },
    )
    assert first.status_code == 200
    cid = next(
        json.loads(line.split("data: ", 1)[1])["conversation_id"]
        for line in first.text.splitlines()
        if line.startswith("data: ") and '"conversation_id"' in line
    )

    followup = c.post(
        "/api/v1/conversations/ask",
        headers=headers,
        json={
            "conversation_id": cid,
            "question": "Try again now",
            "agent": "pi",
            "provider_id": provider_id,
            "model": "model-a",
            "project": "default",
        },
    )
    assert followup.status_code == 200
    assert len(seen) == 2
    assert seen[0].skill is not None and seen[0].skill.name == "dtb-demo"
    assert seen[1].skill is None
    assert [s.name for s in seen[1].mounted_skills] == ["dtb-demo"]
    assert "dtb-demo/scripts/run.py" in dict(seen[1].mounted_skills[0].files)
