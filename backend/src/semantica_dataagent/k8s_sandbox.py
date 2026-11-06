"""Kubernetes session Pod construction and lifecycle."""

import ipaddress
import os
import socket
from pathlib import PurePosixPath
from urllib.parse import urlsplit

RUNNER_IMAGE = "172.16.242.124:31640/dataagent-agent-runner:1.2.1"
SESSION_LABEL = "app.kubernetes.io/component=agent-session"


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
                "command": ["node", "-e", "setInterval(() => {}, 2147483647)"],
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

    def _run(self, *args, input=None, check=True):
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
            timeout=150,
        )

    def start(self):
        import json

        with self.lock:
            if self.closed:
                raise RuntimeError("session already ended")
            (self.workspace / "work").mkdir(parents=True, exist_ok=True)
            (self.workspace / "sessions").mkdir(parents=True, exist_ok=True)
            try:
                self._run("create", "-f", "-", input=json.dumps(self.spec))
                self._run(
                    "wait",
                    "--for=condition=Ready",
                    f"pod/{self.name}",
                    "--timeout=120s",
                )
                self._upload()
                self.ready = True
            except BaseException:
                self.close()
                raise

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
                if any(not (item.isfile() or item.isdir()) for item in archive):
                    raise ValueError("unsupported session archive entry")
                archive.extractall(directory, filter="data")
            for source, target in (("workspace", "work"), ("sessions", "sessions")):
                shutil.copytree(
                    os.path.join(directory, source),
                    self.workspace / target,
                    dirs_exist_ok=True,
                    symlinks=False,
                )

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
