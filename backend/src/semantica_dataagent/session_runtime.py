"""Selection and interface for one turn's execution environment."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from .agent_sandbox import SessionSandbox
from .agent_types import validated_ephemeral_files
from .k8s_sandbox import KubernetesSessionSandbox


class SessionRuntime(Protocol):
    model_url: str
    mcp_url: str

    def __init__(self, workspace: Path, model_url: str, mcp_url: str): ...
    def start(self) -> None: ...
    def close(self) -> None: ...
    def wrap_command(self, command, env, keys): ...
    def stage_ephemeral_files(self, files: tuple[tuple[str, bytes], ...]) -> str: ...


class LocalSessionRuntime:
    """Run directly on the host; intended only for trusted development."""

    def __init__(self, workspace: Path, model_url: str, mcp_url: str):
        self.model_url = model_url
        self.mcp_url = mcp_url
        self._ephemeral_dirs: list[tempfile.TemporaryDirectory[str]] = []

    def start(self) -> None:
        pass

    def stage_ephemeral_files(self, files: tuple[tuple[str, bytes], ...]) -> str:
        entries = validated_ephemeral_files(files)
        temp_dir = tempfile.TemporaryDirectory(prefix="sda-skill-")
        self._ephemeral_dirs.append(temp_dir)
        root = Path(temp_dir.name)
        os.chmod(root, 0o700)
        for rel, content in entries:
            target = root.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400
            )
            try:
                view = memoryview(content)
                written = 0
                while written < len(view):
                    written += os.write(fd, view[written:])
            finally:
                os.close(fd)
        return str(root)

    def close(self) -> None:
        while self._ephemeral_dirs:
            temp_dir = self._ephemeral_dirs.pop()
            shutil.rmtree(temp_dir.name, ignore_errors=True)
            temp_dir.cleanup()

    def wrap_command(self, command, env, keys):
        return command, None


def runtime_class() -> type[SessionRuntime]:
    mode = os.environ.get("DATAAGENT_AGENT_MODE", "sandbox")
    if mode == "local":
        return LocalSessionRuntime
    if mode in ("sandbox", "docker"):
        return SessionSandbox
    if mode == "k8s":
        return KubernetesSessionSandbox
    raise ValueError(f"unknown session runtime: {mode}")


def runtime_available(name: str, binary: str | None = None) -> bool:
    if binary or os.environ.get("DATAAGENT_AGENT_MODE", "sandbox") == "local":
        return bool(binary or shutil.which(name))
    runtime = runtime_class()
    return bool(
        shutil.which("kubectl" if runtime is KubernetesSessionSandbox else "docker")
    )
