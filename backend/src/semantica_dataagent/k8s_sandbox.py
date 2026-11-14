"""Kubernetes session Pod construction and lifecycle."""

import ipaddress
import os
import re
import socket
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .failure_diagnostics import (
    safe_pod_status,
    safe_session_category,
    safe_session_stage,
)

RUNNER_IMAGE = "172.16.242.124:31640/dataagent-agent-runner:1.2.1"
SESSION_LABEL = "app.kubernetes.io/component=agent-session"
POD_STATUS_TIMEOUT_SECONDS = 10

_NODE_NAME = re.compile(
    r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*"
)

# Node ignores default signal dispositions when it runs as PID 1.  Without an
# explicit handler a normal Pod deletion waits out the 30 second grace period
# and then kills the container (exit 137).
PID1_SIGTERM_HANDLER = "process.on('SIGTERM', () => process.exit(0)); "
PID1_COMMAND = [
    "node",
    "-e",
    PID1_SIGTERM_HANDLER + "setInterval(() => {}, 2147483647)",
]


class SessionStartError(RuntimeError):
    """A safe start-stage failure without kubectl output or pod contents."""

    def __init__(self, stage, category, *, pod_status=None):
        self.failure_stage = safe_session_stage(stage)
        self.failure_category = safe_session_category(category)
        self.pod_status = safe_pod_status(pod_status)
        super().__init__(
            f"session start failed: {self.failure_stage}/{self.failure_category}"
        )


def _endpoint(address: str) -> tuple[str, int]:
    url = urlsplit(address)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
    ):
        raise ValueError("invalid agent endpoint")
    return url.hostname, url.port or (443 if url.scheme == "https" else 80)


def _resolve(address: str) -> tuple[str, int, list[str]]:
    host, port = _endpoint(address)
    addresses = sorted(
        {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    )
    if not addresses:
        raise ValueError(f"cannot resolve agent endpoint: {host}")
    return host, port, addresses


def _session_nodes(value: str) -> list[str]:
    nodes = value.split(",")
    if any(
        not node
        or node.strip() != node
        or len(node) > 63
        or _NODE_NAME.fullmatch(node) is None
        for node in nodes
    ):
        raise ValueError("invalid DATAAGENT_SESSION_NODES")
    return nodes


def _firewall(endpoints) -> str:
    rules = []
    for _, port, addresses in endpoints:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            command = "iptables" if ip.version == 4 else "ip6tables"
            rules.append(
                f"{command} -A OUTPUT -p tcp -m conntrack --ctorigdst {ip} --dport {port} -j ACCEPT"
            )
    return "\n".join(
        [
            "set -eu",
            "iptables -P INPUT DROP",
            "iptables -P OUTPUT DROP",
            "ip6tables -P INPUT DROP",
            "ip6tables -P OUTPUT DROP",
            "iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            "ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            "iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            "ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            *rules,
        ]
    )


def build_pod_spec(
    *, name: str, workspace_subpath: str, model_url: str, mcp_url: str
) -> dict:
    path = PurePosixPath(workspace_subpath)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != "conversations"
        or path.parts[1] in (".", "..")
    ):
        raise ValueError("invalid conversation workspace")
    endpoints = [_resolve(model_url), _resolve(mcp_url)]
    aliases = {}
    for host, _, addresses in endpoints:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            for address in addresses:
                aliases.setdefault(address, []).append(host)
    spec = {
        "hostAliases": [
            {"ip": ip, "hostnames": sorted(set(hosts))} for ip, hosts in aliases.items()
        ],
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {"fsGroup": 1000},
        "initContainers": [
            {
                "name": "firewall",
                "image": RUNNER_IMAGE,
                "command": ["sh", "-ec"],
                "args": [_firewall(endpoints)],
                "securityContext": {
                    "runAsUser": 0,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]},
                },
            }
        ],
        "containers": [
            {
                "name": "agent",
                "image": RUNNER_IMAGE,
                "command": list(PID1_COMMAND),
                "workingDir": "/workspace",
                "securityContext": {
                    "runAsUser": 1000,
                    "runAsGroup": 1000,
                    "runAsNonRoot": True,
                    "readOnlyRootFilesystem": True,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {"limits": {"cpu": "2", "memory": "2Gi"}},
                "volumeMounts": [
                    {"name": "work", "mountPath": "/workspace"},
                    {"name": "sessions", "mountPath": "/sessions"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "work", "emptyDir": {}},
            {"name": "sessions", "emptyDir": {}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    nodes = os.environ.get("DATAAGENT_SESSION_NODES")
    if nodes is not None:
        spec["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": _session_nodes(nodes),
                                }
                            ]
                        }
                    ]
                }
            }
        }
    else:
        node = os.environ.get("DATAAGENT_NODE_NAME")
        if node:
            spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": os.environ.get(
                "DATAAGENT_SESSION_NAMESPACE", "semantica-dataagent-sessions"
            ),
            "labels": {"app.kubernetes.io/component": "agent-session"},
        },
        "spec": spec,
    }


# The first stdin line carries the environment; neither credentials nor tokens
# appear in a Pod specification, kubectl arguments or a persisted workspace file.
EXEC_WRAPPER = r"""
const {spawn} = require('child_process');
let pending = Buffer.alloc(0);
function start(chunk) {
  pending = Buffer.concat([pending, chunk]);
  const end = pending.indexOf(10);
  if (end < 0) {
    if (pending.length > 1048576) process.exit(2);
    return;
  }
  process.stdin.removeListener('data', start);
  let env;
  try { env = JSON.parse(pending.subarray(0, end).toString('utf8')); }
  catch (_) { process.exit(2); }
  const child = spawn(process.argv[1], process.argv.slice(2), {
    env: {...process.env, ...env}, stdio: ['pipe', 'inherit', 'inherit']
  });
  child.on('error', () => process.exit(127));
  child.stdin.on('error', () => {});
  child.on('exit', (code) => process.exit(code === null ? 1 : code));
  const rest = pending.subarray(end + 1);
  if (rest.length) child.stdin.write(rest);
  process.stdin.pipe(child.stdin);
}
process.stdin.on('data', start);
"""


class KubernetesSessionSandbox:
    def __init__(self, workspace, model_url, mcp_url):
        import threading
        import uuid
        from pathlib import Path

        self.workspace = Path(workspace).resolve()
        root = Path(
            os.environ.get("DATAAGENT_AGENT_ROOT", "/data/conversations")
        ).resolve()
        if self.workspace.parent != root:
            raise ValueError("workspace must be a direct child of /data/conversations")
        self.name = "sda-agent-" + uuid.uuid4().hex
        self.model_url = model_url
        self.mcp_url = mcp_url
        self.lock = threading.RLock()
        self.closed = False
        self.ready = False
        self.spec = build_pod_spec(
            name=self.name,
            workspace_subpath=f"conversations/{self.workspace.name}",
            model_url=model_url,
            mcp_url=mcp_url,
        )

    def _run(self, *args, input=None, check=True, timeout=150):
        import subprocess

        return subprocess.run(
            [
                "kubectl",
                "-n",
                os.environ.get(
                    "DATAAGENT_SESSION_NAMESPACE", "semantica-dataagent-sessions"
                ),
                *args,
            ],
            input=input,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
        )

    def _pod_status(self):
        """Read only allow-listed status fields from this session Pod."""
        import json

        try:
            result = self._run(
                "get",
                "pod",
                self.name,
                "-o",
                "json",
                check=False,
                timeout=POD_STATUS_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - diagnostics must not mask start failure
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None
        status = payload.get("status") if isinstance(payload, dict) else None
        if not isinstance(status, dict):
            return None
        safe = {}
        for source, target in (("phase", "phase"), ("reason", "reason")):
            value = status.get(source)
            if isinstance(value, str) and value:
                safe[target] = value
        container_statuses = status.get("containerStatuses")
        if isinstance(container_statuses, list) and container_statuses:
            container = container_statuses[0]
            state = container.get("state") if isinstance(container, dict) else None
            waiting = state.get("waiting") if isinstance(state, dict) else None
            last = container.get("lastState") if isinstance(container, dict) else None
            terminated = last.get("terminated") if isinstance(last, dict) else None
            reason = (
                waiting.get("reason")
                if isinstance(waiting, dict)
                else terminated.get("reason")
                if isinstance(terminated, dict)
                else None
            )
            if isinstance(reason, str) and reason:
                safe["container_reason"] = reason
        return safe_pod_status(safe)

    def _start_failure(self, exc, stage):
        import subprocess

        if not isinstance(exc, Exception):
            return exc
        if isinstance(exc, subprocess.TimeoutExpired):
            category = "timeout"
        elif isinstance(exc, subprocess.CalledProcessError):
            category = "nonzero_exit"
        else:
            category = "startup_error"
        pod_status = self._pod_status() if stage in {"create", "wait"} else None
        return SessionStartError(stage, category, pod_status=pod_status)

    def start(self):
        import json

        with self.lock:
            if self.closed:
                raise RuntimeError("session already ended")
            (self.workspace / "work").mkdir(parents=True, exist_ok=True)
            (self.workspace / "sessions").mkdir(parents=True, exist_ok=True)
            stage = "create"
            try:
                self._run("create", "-f", "-", input=json.dumps(self.spec))
                stage = "wait"
                self._run(
                    "wait",
                    "--for=condition=Ready",
                    f"pod/{self.name}",
                    "--timeout=120s",
                )
                stage = "upload"
                self._upload()
                self.ready = True
            except BaseException as exc:
                diagnostic_cancel = None
                try:
                    failure = self._start_failure(exc, stage)
                except Exception:  # noqa: BLE001 - diagnostics must not mask the first failure
                    failure = exc
                except BaseException as cancelled:  # noqa: BLE001 - cancellation wins
                    failure = exc
                    diagnostic_cancel = cancelled
                try:
                    self.close()
                except Exception:  # noqa: BLE001, S110 - first failure wins
                    pass
                if diagnostic_cancel is not None:
                    raise diagnostic_cancel from None
                if failure is exc:
                    raise
                raise failure from None

    def _transfer(self, direction, payload=None):
        import subprocess

        command = [
            "kubectl",
            "-n",
            os.environ.get(
                "DATAAGENT_SESSION_NAMESPACE", "semantica-dataagent-sessions"
            ),
            "exec",
            "-i",
            self.name,
            "-c",
            "agent",
            "--",
            "tar",
        ]
        command += (
            ["--no-overwrite-dir", "--no-same-owner", "-xf", "-", "-C", "/"]
            if direction == "upload"
            else ["-cf", "-", "-C", "/", "workspace", "sessions"]
        )
        return subprocess.run(
            command, input=payload, capture_output=True, check=True, timeout=150
        ).stdout

    def _upload(self):
        import io
        import tarfile

        data = io.BytesIO()

        def regular_only(member):
            if member.name in ("workspace", "sessions"):
                return None
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported session file: {member.name}")
            return member

        with tarfile.open(fileobj=data, mode="w") as archive:
            for source, name in (("work", "workspace"), ("sessions", "sessions")):
                for child in (self.workspace / source).iterdir():
                    archive.add(
                        child, arcname=f"{name}/{child.name}", filter=regular_only
                    )
        self._transfer("upload", data.getvalue())

    def _download(self):
        import io
        import shutil
        import tarfile
        import tempfile

        data = self._transfer("download")
        with tempfile.TemporaryDirectory(dir=self.workspace) as directory:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                members = archive.getmembers()
                roots = {item.name.rstrip("/") for item in members if item.isdir()}
                if not {"workspace", "sessions"} <= roots:
                    raise ValueError("incomplete session archive")
                if any(
                    not (item.isfile() or item.isdir())
                    or item.name.split("/", 1)[0] not in {"workspace", "sessions"}
                    for item in members
                ):
                    raise ValueError("unsupported session archive entry")
                archive.extractall(directory, filter="data")
            for source, target in (("workspace", "work"), ("sessions", "sessions")):
                source_path = Path(directory) / source
                target_path = self.workspace / target
                for current, dirs, files in os.walk(source_path):
                    relative = Path(current).relative_to(source_path)
                    for name in dirs:
                        existing = target_path / relative / name
                        if existing.is_symlink() or (
                            existing.exists() and not existing.is_dir()
                        ):
                            existing.unlink()
                    for name in files:
                        existing = target_path / relative / name
                        if existing.is_symlink():
                            existing.unlink()
                        elif existing.is_dir():
                            shutil.rmtree(existing)
                shutil.copytree(
                    source_path, target_path, dirs_exist_ok=True, symlinks=False
                )
                for current, dirs, files in os.walk(target_path, topdown=False):
                    current_path = Path(current)
                    relative = current_path.relative_to(target_path)
                    for name in files:
                        if not (source_path / relative / name).exists():
                            (current_path / name).unlink()
                    for name in dirs:
                        if not (source_path / relative / name).exists():
                            shutil.rmtree(current_path / name)

    def wrap_command(self, command, env, keys):
        import json

        for key in ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"):
            if key in os.environ:
                env[key] = os.environ[key]
        values = {key: env[key] for key in keys if key in env}
        header = (json.dumps(values, ensure_ascii=True) + "\n").encode()
        return [
            "kubectl",
            "-n",
            os.environ.get(
                "DATAAGENT_SESSION_NAMESPACE", "semantica-dataagent-sessions"
            ),
            "exec",
            "-i",
            self.name,
            "-c",
            "agent",
            "--",
            "node",
            "-e",
            EXEC_WRAPPER,
            *command,
        ], header

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                if self.ready:
                    self._download()
            finally:
                self._run(
                    "delete",
                    "pod",
                    self.name,
                    "--ignore-not-found",
                    "--wait=true",
                    "--timeout=120s",
                )


def cleanup_orphans():
    import subprocess

    subprocess.run(
        [
            "kubectl",
            "-n",
            os.environ.get(
                "DATAAGENT_SESSION_NAMESPACE", "semantica-dataagent-sessions"
            ),
            "delete",
            "pods",
            "-l",
            SESSION_LABEL,
            "--wait=true",
            "--timeout=120s",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=150,
    )
