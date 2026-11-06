"""Exercise local MCP confirmation against an isolated fake executor."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def check_local_mcp_query(ctx):
    from __main__ import McpSession

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"status": 200, "data": {"queryKey": "fake"}}).encode()
            )

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "status": 200,
                        "data": {
                            "progress": 1,
                            "columns": [{"name": "sales", "type": "long"}],
                            "data": [[12345]],
                        },
                    }
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    other_session = None
    try:
        response = ctx.rest.put(
            "/api/v1/plugins/executor/dtb",
            json={
                "enabled": True,
                "parameters": {
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "row_limit": 100,
                    "download_limit": 100,
                    "timeout_seconds": 5,
                },
            },
        )
        ctx.require(response.status_code == 200, response.text)
        response = ctx.rest.put("/api/v1/me/dtb-token", json={"token": "fake"})
        ctx.require(response.status_code == 200, response.text)
        plan = ctx.mcp_call(
            "execute_query",
            {
                "sql": "SELECT sum(amount) AS sales FROM shop.orders WHERE dt = '2025-01-01'"
            },
        )
        query_id = plan["id"]
        executions = lambda: [
            x for x in calls if "_dataagent_query" in x.get("sql", "")
        ]
        ctx.require(not executions(), "executed before confirmation")
        other = ctx.rest.post(
            "/api/v1/tokens", json={"name": "issue-82-other", "role": "user"}
        )
        ctx.require(other.status_code == 201, other.text)
        other_session = McpSession(ctx.runtime.base_url, other.json()["token"])
        other_session.open(30)
        rejected = other_session.call_tool(
            "execute_query", {"confirmation_id": query_id}
        )
        ctx.require(
            rejected.isError and not executions(), "other token confirmed query"
        )
        result = ctx.mcp_call("execute_query", {"confirmation_id": query_id})
        ctx.require(
            result["columns"] == ["sales"] and result["rows"] == [[12345]], str(result)
        )
        ctx.require(len(executions()) == 1, str(calls))
        for fmt, url in result["download_urls"].items():
            path = f"/api/v1/queries/{query_id}/download?format={fmt}"
            download = ctx.rest.get(path)
            ctx.require(
                download.status_code == 200 and download.content,
                f"download failed: {fmt}",
            )
            denied = ctx.rest.get(
                path, headers={"Authorization": "Bearer " + other.json()["token"]}
            )
            ctx.require(denied.status_code == 404, "other token downloaded result")
            ctx.require(url.endswith(path), f"bad URL: {url}")
        ctx.require(
            ctx.rest.get(f"/api/v1/queries/{query_id}/download?format=csv").content
            == b"sales\n12345\n",
            "wrong CSV",
        )
        ctx.require(
            ctx.mcp_call(
                "execute_query", {"confirmation_id": query_id}, allow_error=True
            ).isError,
            "confirmation replayed",
        )
        return {"rows": result["rows"]}
    finally:
        if other_session:
            other_session.close()
        server.shutdown()
        thread.join()
        server.server_close()
