"""Exercise latest-partition subqueries through REST plan and execution."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def check_latest_partition_query(ctx):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            payload = json.dumps(
                {"status": 200, "data": {"queryKey": "issue-120"}}
            ).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            payload = json.dumps(
                {
                    "status": 200,
                    "data": {
                        "progress": 1,
                        "columns": [{"name": "dt", "type": "string"}],
                        "data": [["2025-01-01"]],
                    },
                }
            ).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = ctx.rest.put(
            "/api/v1/plugins/executor/dtb",
            json={
                "enabled": True,
                "parameters": {
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "row_limit": 10,
                    "download_limit": 50,
                    "timeout_seconds": 5,
                },
            },
        )
        ctx.require(response.status_code == 200, response.text)
        response = ctx.rest.put("/api/v1/me/dtb-token", json={"token": "fake"})
        ctx.require(response.status_code == 200, response.text)

        sql = (
            "SELECT o.id, o.dt FROM shop.orders AS o "
            "WHERE o.dt = (SELECT MAX(dt) FROM shop.orders) "
            "AND o.dt >= '2025-01-01'"
        )
        response = ctx.rest.post("/api/v1/queries/plan", json={"sql": sql})
        ctx.require(response.status_code == 200, response.text)
        plan = response.json()
        ctx.require(
            plan["partition_selection"] == "latest_at_runtime",
            f"latest partition not marked: {plan}",
        )
        ctx.require(
            plan["estimated_partitions"] is None and plan["estimated_rows"] is None,
            f"latest partition estimate was fabricated: {plan}",
        )

        rejected = ctx.rest.post(
            "/api/v1/queries/plan",
            json={
                "sql": "SELECT * FROM shop.orders WHERE dt >= "
                "(SELECT MAX(dt) FROM shop.orders)"
            },
        )
        ctx.require(
            rejected.status_code == 422 and "缺少分区条件" in rejected.text,
            f"non-equality latest partition was accepted: {rejected.text}",
        )

        response = ctx.rest.post(
            f"/api/v1/queries/{plan['id']}/execute", json={"limit": 1}
        )
        ctx.require(response.status_code == 200, response.text)
        executions = [
            call for call in calls if "_dataagent_query" in call.get("sql", "")
        ]
        ctx.require(len(executions) == 1, f"DTB execution count: {calls}")
        bounded = executions[0]["sql"]
        ctx.require(
            bounded.startswith("SELECT * FROM (") and "MAX(dt)" in bounded,
            f"query was not wrapped as a subquery: {bounded}",
        )
        ctx.require(
            bounded.rstrip().endswith(") AS _dataagent_query LIMIT 50"),
            f"outermost LIMIT missing: {bounded}",
        )
        return {"plan_id": plan["id"], "bounded": bounded}
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
