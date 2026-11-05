"""Docker integration contract for per-conversation isolation."""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from semantica_dataagent import agent_runner  # noqa: F401
from semantica_dataagent import pi_runner


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
        script = """import json, os, socket, urllib.request

def reachable(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False

print(json.dumps({
    "host_home": os.path.exists("/home/ub"),
    "host_secret": os.path.exists(%r),
    "source": os.path.exists(%r),
    "application": os.path.exists(%r),
    "external": reachable("https://example.com"),
    "host_port": reachable("http://host.docker.internal:%s"),
    "model": reachable(os.environ["DATAAGENT_MODEL_URL"]),
    "mcp": reachable(os.environ["DATAAGENT_MCP_URL"]),
}))
""" % (
            str(marker),
            str(Path.cwd()),
            str(Path(pi_runner.__file__)),
            servers[2].server_port,
        )
        result = json.loads(sandbox.exec(["python3", "-c", script]))
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
