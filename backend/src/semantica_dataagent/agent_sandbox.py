"""Disposable Docker session and strictly scoped TCP egress relay."""

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

IMAGE = "semantica-dataagent-agent-runner:1.2.0"
LABEL = "com.semantica-dataagent.agent=1"
OWNER = "com.semantica-dataagent.owner"
RELAY = r"""const net = require('net');
const http = require('http');
const targets = JSON.parse(process.env.TARGETS);
for (const [port, host, target, scheme, authority] of targets) {
  if (scheme === 'https') {
    net.createServer(client => {
      const upstream = net.connect(target, host);
      client.on('error', () => upstream.destroy());
      upstream.on('error', () => client.destroy());
      client.pipe(upstream).pipe(client);
    }).listen(port, '0.0.0.0');
  } else {
    http.createServer((req, res) => {
      const upstream = http.request({host, port: target, method: req.method,
        path: req.url, headers: {...req.headers, host: authority}}, response => {
        res.writeHead(response.statusCode, response.headers);
        response.pipe(res);
      });
      upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
      req.pipe(upstream);
    }).listen(port, '0.0.0.0');
  }
}
"""


class SessionSandbox:
    def __init__(self, workspace: Path, model_url: str, mcp_url: str):
        self.workspace = workspace.resolve()
        self.name = f"sda-agent-{uuid.uuid4().hex}"
        self.network = self.name
        self.proxy = self.name + "-relay"
        self.container_id = self.name
        self.owner = str(os.getpid())
        self.lock = threading.RLock()
        self.closed = False
        self.targets = []
        self.aliases = {"sda-relay"}
        self.model_url = self._route(model_url, 18080)
        self.mcp_url = self._route(mcp_url, 18081)

    def _route(self, address: str, port: int) -> str:
        url = urlsplit(address)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("invalid agent endpoint")
        host = url.hostname
        target = (
            "host.docker.internal"
            if host in ("127.0.0.1", "localhost", "::1")
            else min(
                socket.getaddrinfo(
                    host,
                    url.port or (443 if url.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                ),
                key=lambda entry: entry[0] != socket.AF_INET,
            )[4][0]
        )
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        alias = "sda-relay" if target == "host.docker.internal" or literal else host
        self.aliases.add(alias)
        self.targets.append(
            [
                port,
                target,
                url.port or (443 if url.scheme == "https" else 80),
                url.scheme,
                url.netloc,
            ]
        )
        return urlunsplit(
            (url.scheme, f"{alias}:{port}", url.path, url.query, url.fragment)
        )

    def _run(self, *args, check=True):
        return subprocess.run(
            ["docker", *args], check=check, capture_output=True, text=True
        )

    def start(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("session already ended")
            self._start()

    def _start(self):
        (self.workspace / "work").mkdir(parents=True, exist_ok=True)
        (self.workspace / "sessions").mkdir(parents=True, exist_ok=True)
        resolv = self.workspace / "agent-resolv.conf"
        resolv.write_text("nameserver 127.0.0.1\n", encoding="ascii")
        try:
            if any(target[1] == "host.docker.internal" for target in self.targets):
                resolved = self._run(
                    "run",
                    "--rm",
                    "--network",
                    "bridge",
                    "--entrypoint",
                    "node",
                    IMAGE,
                    "-e",
                    "require('dns').lookup('host.docker.internal', (err, address) => { if (err) process.exit(1); console.log(address); })",
                ).stdout.strip()
                ipaddress.ip_address(resolved)
                for target in self.targets:
                    if target[1] == "host.docker.internal":
                        target[1] = resolved
            self._run(
                "network",
                "create",
                "--internal",
                "--label",
                LABEL,
                "--label",
                f"{OWNER}={self.owner}",
                self.network,
            )
            aliases = [
                arg for alias in self.aliases for arg in ("--network-alias", alias)
            ]
            self._run(
                "create",
                "--name",
                self.proxy,
                "--label",
                LABEL,
                "--label",
                f"{OWNER}={self.owner}",
                "--network",
                self.network,
                *aliases,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--env",
                "TARGETS=" + json.dumps(self.targets),
                "--entrypoint",
                "node",
                IMAGE,
                "-e",
                RELAY,
            )
            self._run("network", "connect", "bridge", self.proxy)
            self._run("start", self.proxy)
            relay_ip = self._run(
                "inspect",
                "--format",
                '{{range .NetworkSettings.Networks}}{{if eq .NetworkID "'
                + self._run(
                    "network", "inspect", "--format", "{{.Id}}", self.network
                ).stdout.strip()
                + '"}}{{.IPAddress}}{{end}}{{end}}',
                self.proxy,
            ).stdout.strip()
            if not relay_ip:
                raise RuntimeError("cannot determine relay address")
            hosts = [
                arg
                for alias in self.aliases
                for arg in ("--add-host", f"{alias}:{relay_ip}")
            ]
            self._run(
                "run",
                "-d",
                "--name",
                self.name,
                "--label",
                LABEL,
                "--label",
                f"{OWNER}={self.owner}",
                "--network",
                self.network,
                "--dns",
                "127.0.0.1",
                *hosts,
                "--mount",
                f"type=bind,src={resolv},dst=/etc/resolv.conf,readonly",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--cpus",
                os.environ.get("DATAAGENT_AGENT_CPUS", "2"),
                "--memory",
                os.environ.get("DATAAGENT_AGENT_MEMORY", "2g"),
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--mount",
                f"type=bind,src={self.workspace / 'work'},dst=/workspace",
                "--mount",
                f"type=bind,src={self.workspace / 'sessions'},dst=/sessions",
                "--workdir",
                "/workspace",
                "--entrypoint",
                "node",
                IMAGE,
                "-e",
                "setInterval(() => {}, 2147483647)",
            )
        except BaseException:
            self.close()
            raise

    def wrap_command(self, command, env, keys):
        docker = ["docker", "exec", "-i"]
        for key in keys:
            if key in env:
                docker.extend(["--env", key])
        return [*docker, self.name, *command], None

    def exec(self, command):
        return self._run(
            "exec",
            "-e",
            f"DATAAGENT_MODEL_URL={self.model_url}",
            "-e",
            f"DATAAGENT_MCP_URL={self.mcp_url}",
            self.name,
            *command,
        ).stdout

    def close(self):
        with self.lock:
            self.closed = True
            self._run("rm", "-f", self.name, self.proxy, check=False)
            self._run("network", "rm", self.network, check=False)


# Active sessions are removed immediately after a turn, so there is no idle container.
ACTIVE: dict[str, SessionSandbox] = {}
ACTIVE_LOCK = threading.Lock()


def register_session(conversation_id: str, sandbox: SessionSandbox) -> None:
    with ACTIVE_LOCK:
        if conversation_id in ACTIVE:
            raise RuntimeError("conversation already has an active turn")
        ACTIVE[conversation_id] = sandbox


def unregister_session(conversation_id: str, sandbox: SessionSandbox) -> None:
    with ACTIVE_LOCK:
        if ACTIVE.get(conversation_id) is sandbox:
            del ACTIVE[conversation_id]


def end_session(conversation_id: str) -> None:
    with ACTIVE_LOCK:
        sandbox = ACTIVE.pop(conversation_id, None)
    if sandbox is not None:
        sandbox.close()


def cleanup_orphans() -> None:
    if os.environ.get("DATAAGENT_AGENT_MODE") == "k8s":
        from .k8s_sandbox import cleanup_orphans as cleanup_k8s_orphans

        cleanup_k8s_orphans()
        return
    if not shutil.which("docker"):
        return
    for kind, list_args, remove_args in (
        ("container", ["ps", "-aq"], ["rm", "-f"]),
        ("network", ["network", "ls", "-q"], ["network", "rm"]),
    ):
        result = subprocess.run(
            ["docker", *list_args, "--filter", f"label={LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Cannot list agent {kind}s for cleanup")
        for item in result.stdout.split():
            inspect = subprocess.run(
                ["docker", "inspect", "--type", kind, item],
                capture_output=True,
                text=True,
                check=True,
            )
            info = json.loads(inspect.stdout)[0]
            labels = (
                info.get("Labels")
                if kind == "network"
                else info.get("Config", {}).get("Labels")
            )
            owner = (labels or {}).get(OWNER)
            if owner:
                try:
                    os.kill(int(owner), 0)
                except ProcessLookupError:
                    pass
                except (ValueError, PermissionError):
                    continue
                else:
                    continue
            subprocess.run(
                ["docker", *remove_args, item], check=True, capture_output=True
            )
