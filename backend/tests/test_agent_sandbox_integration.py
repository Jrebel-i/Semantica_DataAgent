"""Docker integration contract for per-conversation isolation."""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from semantica_dataagent import (
    agent_runner,  # noqa: F401
    pi_runner,
)


def _docker_available():
    return (
        shutil.which("docker")
        and subprocess.run(
            ["docker", "info"], capture_output=True, check=False
        ).returncode
        == 0
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is required")
def test_session_sandbox_files_network_and_cleanup(tmp_path):
    sandbox_type = getattr(pi_runner, "SessionSandbox", None)
    assert sandbox_type is not None, "pi sessions must run in an isolated container"
    servers = [ThreadingHTTPServer(("0.0.0.0", 0), _Handler) for _ in range(3)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    marker = tmp_path / "host-secret"
    marker.write_text("host-only", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = sandbox_type(
        workspace=workspace,
        model_url=f"http://127.0.0.1:{servers[0].server_port}",
        mcp_url=f"http://127.0.0.1:{servers[1].server_port}/mcp/",
    )
    try:
        sandbox.start()
        script = """const fs = require('fs');
const test = async (url) => {
  try { return (await fetch(url, {signal: AbortSignal.timeout(3000)})).ok; }
  catch { return false; }
};
(async () => console.log(JSON.stringify({
  host_home: fs.existsSync('/home/ub'),
  host_secret: fs.existsSync(%s),
  source: fs.existsSync(%s),
  application: fs.existsSync(%s),
  external: await test('https://example.com'),
  host_port: await test('http://host.docker.internal:%s'),
  model: await test(process.env.DATAAGENT_MODEL_URL),
  mcp: await test(process.env.DATAAGENT_MCP_URL),
})))();
""" % (
            json.dumps(str(marker)),
            json.dumps(str(Path.cwd())),
            json.dumps(str(Path(pi_runner.__file__))),
            servers[2].server_port,
        )  # noqa: UP031
        result = json.loads(sandbox.exec(["node", "-e", script]))
        assert not any(
            result[key] for key in ("host_home", "host_secret", "source", "application")
        ), result
        assert not result["external"] and not result["host_port"], result
        assert result["model"] and result["mcp"], result
        container_id = sandbox.container_id
    finally:
        sandbox.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
    assert (
        subprocess.run(
            ["docker", "inspect", container_id], capture_output=True, check=False
        ).returncode
        != 0
    )
