"""Shared attachment path and image-channel guards for agent runners (#189)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from semantica_dataagent.agent_plugin import (
    AttachmentPathError,
    attachment_path,
    build_attachment_prompt,
    read_attachment_bytes,
    supported_image_attachments,
)
from semantica_dataagent.agent_types import (
    AgentTurnRequest,
    AttachmentInput,
    ProviderConfig,
)
from semantica_dataagent.claude_runner import ClaudeRunner
from semantica_dataagent.codex_runner import CodexRunner

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63fccfc0f01f0004050001027d4c6b0000000049"
    "454e44ae426082"
)


def _attachment(name: str, path: str | None = None, mime: str = "image/png"):
    return AttachmentInput(
        name=name,
        path=path or f"attachments/{name}",
        size=len(PNG),
        mime_type=mime,
        is_image=mime.startswith("image/"),
    )


def _workspace(tmp_path: Path, name: str = "conversation") -> Path:
    workspace = tmp_path / name
    (workspace / "work" / "attachments").mkdir(parents=True)
    return workspace


def _request(workspace: Path, attachments) -> AgentTurnRequest:
    return AgentTurnRequest(
        conversation_id=workspace.name,
        prompt=build_attachment_prompt("看看附件", attachments),
        session_id="00000000-0000-4000-8000-000000000189",
        workspace=workspace,
        provider=ProviderConfig(
            id="provider-one",
            name="Local",
            type="anthropic-messages",
            base_url="http://127.0.0.1:3425",
            models=["model"],
        ),
        model="model",
        mcp_url="http://127.0.0.1:8000/mcp/",
        mcp_token="token",
        attachments=tuple(attachments),
    )


def _fake_runner_script(tmp_path: Path, name: str) -> Path:
    capture = tmp_path / f"{name}-capture.json"
    script = tmp_path / f"fake-{name}.py"
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({{"type": "result", "result": "ok"}}), flush=True)
print(json.dumps({{"type": "thread.started", "thread_id": "thread-189"}}), flush=True)
print(json.dumps({{"type": "item.completed", "item": {{
    "type": "agent_message", "text": "ok",
}}}}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_attachment_path_accepts_file_inside_conversation_work(tmp_path: Path):
    workspace = _workspace(tmp_path)
    stored = workspace / "work" / "attachments" / "chart.png"
    stored.write_bytes(PNG)

    resolved = attachment_path(workspace, _attachment("chart.png"))

    assert resolved == stored.resolve()


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../other/work/attachments/chart.png",
        "attachments/../../other/work/attachments/chart.png",
        "",
    ],
)
def test_attachment_path_rejects_absolute_and_traversal_paths(
    tmp_path: Path, path: str
):
    workspace = _workspace(tmp_path)
    with pytest.raises(AttachmentPathError):
        attachment_path(workspace, _attachment("chart.png", path))


def test_attachment_path_rejects_symlink_to_another_conversation(tmp_path: Path):
    workspace = _workspace(tmp_path, "conversation-b")
    other = _workspace(tmp_path, "conversation-a")
    secret = other / "work" / "attachments" / "secret.png"
    secret.write_bytes(PNG)
    link = workspace / "work" / "attachments" / "chart.png"
    os.symlink(secret, link)

    with pytest.raises(AttachmentPathError):
        attachment_path(workspace, _attachment("chart.png"))


def test_attachment_path_rejects_missing_file(tmp_path: Path):
    workspace = _workspace(tmp_path)
    with pytest.raises(AttachmentPathError):
        attachment_path(workspace, _attachment("missing.png"))


def test_supported_image_attachments_keeps_only_common_web_formats(tmp_path: Path):
    workspace = _workspace(tmp_path)
    attachments = [
        _attachment("chart.png", mime="image/png"),
        _attachment("photo.jpg", mime="image/jpeg"),
        _attachment("scan.heic", mime="image/heic"),
        _attachment("notes.txt", mime="text/plain"),
    ]
    for attachment in attachments:
        (workspace / "work" / attachment.path).write_bytes(PNG)
    request = _request(workspace, attachments)

    supported = supported_image_attachments(request)

    assert [item.name for item in supported] == ["chart.png", "photo.jpg"]


@pytest.mark.parametrize(
    ("agent", "runner_class"),
    [("claude", ClaudeRunner), ("codex", CodexRunner)],
)
def test_runner_rejects_symlinked_attachment_escape(
    tmp_path: Path, agent: str, runner_class
):
    workspace = _workspace(tmp_path, f"{agent}-conversation")
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    link = workspace / "work" / "attachments" / "chart.png"
    os.symlink(secret, link)
    attachment = _attachment("chart.png")
    request = _request(workspace, [attachment])
    script = _fake_runner_script(tmp_path, agent)

    async def collect():
        return [event async for event in runner_class(binary=str(script)).run(request)]

    events = asyncio.run(collect())

    assert events[-1].type == "error"
    assert "附件" in events[-1].data["message"]
    assert secret.read_bytes() == PNG


def test_read_attachment_bytes_reads_regular_file(tmp_path: Path):
    workspace = _workspace(tmp_path)
    stored = workspace / "work" / "attachments" / "chart.png"
    stored.write_bytes(PNG)

    assert read_attachment_bytes(workspace, _attachment("chart.png")) == PNG


def test_read_attachment_bytes_refuses_a_last_component_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import agent_plugin

    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = workspace / "work" / "attachments" / "chart.png"
    os.symlink(outside, link)
    monkeypatch.setattr(agent_plugin, "attachment_path", lambda *_args: link)

    with pytest.raises(AttachmentPathError):
        read_attachment_bytes(workspace, _attachment("chart.png"))


def test_read_attachment_bytes_refuses_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from semantica_dataagent import agent_plugin

    workspace = _workspace(tmp_path)
    directory = workspace / "work" / "attachments" / "folder"
    directory.mkdir()
    monkeypatch.setattr(agent_plugin, "attachment_path", lambda *_args: directory)

    with pytest.raises(AttachmentPathError):
        read_attachment_bytes(workspace, _attachment("folder"))
