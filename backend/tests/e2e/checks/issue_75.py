"""Exercise query safety and caller credentials through REST and a fake DTB."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def check_query_executor(ctx):
    calls = []

    class DtbHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            token = self.headers.get("token")
            calls.append((token, body))
            if token == "expired":
                self.send_response(401)
                self.end_headers()
                return
            payload = json.dumps({"status": 200, "data": {"queryKey": token}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if "/result/" in self.path:
                progress = 0 if self.path.endswith("/slow") else 1
                payload = json.dumps(
                    {
                        "status": 200,
                        "data": {
                            "progress": progress,
                            "columns": ["n"],
                            "data": [[1], [2]],
                        },
                    }
                ).encode()
            else:
                payload = b"n\n1\n2\n"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), DtbHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:

        def require(response, code):
            ctx.require(
                response.status_code == code,
                f"expected {code}, got {response.status_code}: {response.text}",
            )
            return response

        response = ctx.rest.put(
            "/api/v1/plugins/executor/dtb",
            json={
                "enabled": True,
                "parameters": {
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "row_limit": 1,
                    "download_limit": 5,
                    "timeout_seconds": 1,
                },
            },
        )
        require(response, 200)
        for sql in (
            "DELETE FROM shop.orders",
            "SELECT * FROM shop.orders",
            "SELECT * FROM shop.orders WHERE dt = '2025' OR 1=1",
        ):
            require(ctx.rest.post("/api/v1/queries/plan", json={"sql": sql}), 422)

        def plan():
            response = require(
                ctx.rest.post(
                    "/api/v1/queries/plan",
                    json={"sql": "SELECT * FROM shop.orders WHERE dt = '2025-01-01'"},
                ),
                200,
            )
            data = response.json()
            ctx.require(
                data["data_source"] == "doris_ai" and data["estimated_rows"] is None,
                "plan data source or estimate incorrect",
            )
            return "/api/v1/queries/" + data["id"]

        path = plan()
        require(ctx.rest.post(path + "/execute", json={"limit": 1}), 400)
        response = require(
            ctx.rest.put("/api/v1/me/dtb-token", json={"token": "caller-a"}), 200
        )
        ctx.require("caller-a" not in response.text, "token was echoed")
        require(ctx.rest.post(path + "/execute", json={"limit": 2}), 422)
        response = require(ctx.rest.post(path + "/execute", json={"limit": 1}), 200)
        ctx.require(response.json()["rows"] == [[1]], "preview limit not enforced")
        ctx.require(
            calls[-1][0] == "caller-a"
            and calls[-1][1]["dataSource"] == "doris_ai"
            and calls[-1][1]["defaultLimit"] == 5,
            "DTB request missing caller token or limit",
        )
        ctx.require(
            require(ctx.rest.get(path), 200).json()["status"] == "succeeded",
            "result status missing",
        )
        ctx.require(
            require(ctx.rest.get(path + "/download"), 200).content == b"n\n1\n2\n",
            "download differs",
        )
        other = require(
            ctx.rest.post(
                "/api/v1/tokens", json={"name": "e2e-query-user", "role": "user"}
            ),
            201,
        ).json()["token"]
        user_headers = {"Authorization": f"Bearer {other}"}
        ctx.require(
            require(
                ctx.rest.get("/api/v1/me/dtb-token", headers=user_headers), 200
            ).json()["configured"]
            is False,
            "credentials leaked across callers",
        )
        require(
            ctx.rest.put(
                "/api/v1/me/dtb-token", headers=user_headers, json={"token": "caller-b"}
            ),
            200,
        )
        response = require(
            ctx.rest.post(
                "/api/v1/queries/plan",
                headers=user_headers,
                json={"sql": "SELECT * FROM shop.orders WHERE dt = '2025-01-01'"},
            ),
            200,
        )
        other_path = "/api/v1/queries/" + response.json()["id"]
        require(ctx.rest.get(other_path), 404)
        require(
            ctx.rest.post(
                other_path + "/execute", headers=user_headers, json={"limit": 1}
            ),
            200,
        )
        ctx.require(calls[-1][0] == "caller-b", "executor used another caller token")
        for token, expected in [("expired", 401), ("slow", 504)]:
            require(ctx.rest.put("/api/v1/me/dtb-token", json={"token": token}), 200)
            response = require(
                ctx.rest.post(plan() + "/execute", json={"limit": 1}), expected
            )
            if token == "expired":
                ctx.require(
                    "token" in response.text.lower(),
                    "401 does not suggest updating token",
                )
        audit = require(ctx.rest.get("/api/v1/query-audit"), 200).json()
        ctx.require(
            {
                "succeeded",
                "rejected_400",
                "rejected_422",
                "rejected_401",
                "rejected_504",
            }.issubset({row["status"] for row in audit}),
            "missing audit records",
        )
        return {"executions": len(audit)}
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
