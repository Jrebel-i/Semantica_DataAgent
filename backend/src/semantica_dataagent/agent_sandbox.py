"""Disposable Docker session and strictly scoped TCP egress relay."""

import json
import os
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

IMAGE = "semantica-dataagent-agent-runner:1.2.0"
LABEL = "com.semantica-dataagent.agent=1"
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
            else host
        )
        alias = "sda-relay" if target == "host.docker.internal" else host
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
        (self.workspace / "work").mkdir(parents=True, exist_ok=True)
        (self.workspace / "sessions").mkdir(parents=True, exist_ok=True)
        try:
            self._run("network", "create", "--internal", "--label", LABEL, self.network)
            aliases = [
                arg for alias in self.aliases for arg in ("--network-alias", alias)
            ]
            self._run(
                "create",
                "--name",
                self.proxy,
                "--label",
                LABEL,
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
            self._run(
                "run",
                "-d",
                "--name",
                self.name,
                "--label",
                LABEL,
                "--network",
                self.network,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "512",
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
        self._run("rm", "-f", self.name, self.proxy, check=False)
        self._run("network", "rm", self.network, check=False)
