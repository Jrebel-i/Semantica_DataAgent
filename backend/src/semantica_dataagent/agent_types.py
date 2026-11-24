"""Shared online agent protocol and event types."""

import io
import tarfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

TEXT_DELTA = "text_delta"
TOOL_START = "tool_start"
TOOL_END = "tool_end"
CLARIFICATION = "clarification"
QUERY_PLAN = "query_plan"
QUERY_RESULT = "query_result"
DONE = "done"
ERROR = "error"
EVENT_TYPES = (
    TEXT_DELTA,
    TOOL_START,
    TOOL_END,
    CLARIFICATION,
    QUERY_PLAN,
    QUERY_RESULT,
    DONE,
    ERROR,
)


@dataclass(slots=True)
class AgentEvent:
    type: str
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.type not in EVENT_TYPES:
            raise ValueError(f"Unknown agent event type: {self.type}")

    def to_dict(self) -> dict:
        return {"type": self.type, **self.data}


@dataclass(slots=True)
class ProviderConfig:
    id: str
    name: str
    type: str
    base_url: str
    models: list[str]
    api_key: str | None = None


@dataclass(slots=True)
class AttachmentInput:
    """One attachment already stored in the conversation workspace."""

    name: str
    path: str
    size: int
    mime_type: str
    is_image: bool


@dataclass(slots=True)
class AgentTurnRequest:
    conversation_id: str
    prompt: str
    session_id: str
    workspace: Path
    provider: ProviderConfig
    model: str
    mcp_url: str
    mcp_token: str
    turn_id: int | None = None
    effort: str | None = None
    project_type: str = "warehouse"
    attachments: tuple[AttachmentInput, ...] = ()
    skill: Any | None = None
    skill_secrets: dict[str, str] = field(default_factory=dict)


class AgentRunner(Protocol):
    agent_id: str
    effort_levels: tuple[str, ...]
    supports_skills: bool

    def available(self) -> bool: ...

    def run(self, request: AgentTurnRequest) -> AsyncIterator[AgentEvent]: ...


def validated_ephemeral_files(
    files: Iterable[tuple[str, bytes]],
) -> tuple[tuple[PurePosixPath, bytes], ...]:
    """Validate and sort (relpath, bytes) pairs before ephemeral staging."""
    entries: list[tuple[PurePosixPath, bytes]] = []
    seen: set[str] = set()
    dir_prefixes: set[str] = set()
    for raw_path, content in files:
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or "\\" in raw_path
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c == ":" for c in raw_path)
        ):
            raise ValueError("Invalid ephemeral file path")
        rel = PurePosixPath(raw_path)
        if (
            rel.is_absolute()
            or not rel.parts
            or any(
                part in {"", ".", ".."} or part.startswith(".") for part in rel.parts
            )
        ):
            raise ValueError("Invalid ephemeral file path")
        folded = rel.as_posix().casefold()
        if folded in seen:
            raise ValueError("Duplicate ephemeral file path")
        seen.add(folded)
        for index in range(1, len(rel.parts)):
            dir_prefixes.add("/".join(rel.parts[:index]).casefold())
        entries.append((rel, bytes(content)))
    if seen & dir_prefixes:
        raise ValueError("Ephemeral file conflicts with directory")
    return tuple(sorted(entries, key=lambda item: item[0].as_posix()))


def ephemeral_tar_bytes(entries: tuple[tuple[PurePosixPath, bytes], ...]) -> bytes:
    """Build a deterministic regular-file-only POSIX tar archive in memory."""
    buf = io.BytesIO()
    dirs_emitted: set[str] = set()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for rel, content in entries:
            for index in range(1, len(rel.parts)):
                parent = "/".join(rel.parts[:index])
                if parent in dirs_emitted:
                    continue
                dirs_emitted.add(parent)
                dir_info = tarfile.TarInfo(name=parent)
                dir_info.type = tarfile.DIRTYPE
                dir_info.mode = 0o755
                archive.addfile(dir_info)
            info = tarfile.TarInfo(name=rel.as_posix())
            info.type = tarfile.REGTYPE
            info.mode = 0o444
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buf.getvalue()
