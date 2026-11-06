"""Selection and interface for one turn's execution environment."""

import os
import shutil
from pathlib import Path
from typing import Protocol

from .agent_sandbox import SessionSandbox
from .k8s_sandbox import KubernetesSessionSandbox


class SessionRuntime(Protocol):
    model_url: str
    mcp_url: str

    def __init__(self, workspace: Path, model_url: str, mcp_url: str): ...
    def start(self) -> None: ...
    def close(self) -> None: ...
    def wrap_command(self, command, env, keys): ...


def runtime_class() -> type[SessionRuntime]:
    mode = os.environ.get("DATAAGENT_AGENT_MODE", "sandbox")
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
