"""End-to-end checks for metadata-only telemetry (#234)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LANGFUSE_SECRET = "issue-234-dummy-secret"


class Receiver:
    """A local Langfuse-compatible ingestion endpoint."""

    def __init__(self):
        self.bodies = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                owner.bodies.append(json.loads(self.rfile.read(length)))
                payload = b'{"success": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def spans(self, name):
        found = []
        for request in list(self.bodies):
            for event in request.get("batch", []):
                body = event.get("body") or {}
                if body.get("name") == name:
                    found.append(body)
        return found

    def wait_spans(self, name, count, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            spans = self.spans(name)
            if len(spans) >= count:
                return spans
            time.sleep(0.1)
        raise AssertionError(
            f"expected {count} telemetry spans named {name}, got {len(self.spans(name))}"
        )


def _configure(ctx, receiver, capture=None):
    values = {
        "langfuse_enabled": "true",
        "langfuse_url": receiver.url,
        "langfuse_public_key": "issue-234-public",
        "langfuse_secret_key": LANGFUSE_SECRET,
    }
    if capture is not None:
        values["langfuse_capture_content"] = capture
    response = ctx.rest.put("/api/v1/settings", json={"values": values})
    ctx.require(response.status_code == 200, f"settings HTTP {response.status_code}")


def check_telemetry_defaults_to_metadata(ctx):
    literal_one = "issue-234-literal-one"
    literal_two = "issue-234-literal-two"
    sql_one = (
        f"SELECT id FROM shop.orders WHERE dt = '2025-01-01' AND id = '{literal_one}'"
    )
    sql_two = (
        f"SELECT id FROM shop.orders WHERE dt = '2026-02-02' AND id = '{literal_two}'"
    )
    sql_other = (
        "SELECT created_at FROM shop.orders "
        f"WHERE dt = '2025-01-01' AND id = '{literal_one}'"
    )
    receiver = Receiver().start()
    try:
        _configure(ctx, receiver)
        for sql in (sql_one, sql_two, sql_other):
            ctx.mcp_call("validate_sql", {"sql": sql, "dialect": "hive"})
        spans = receiver.wait_spans("validate_sql", 3)

        fingerprints = []
        for span in spans:
            encoded = json.dumps(span, ensure_ascii=False)
            for literal in (literal_one, literal_two, "2025-01-01", "2026-02-02"):
                ctx.require(
                    literal not in encoded,
                    f"literal leaked in default telemetry mode: {literal}",
                )
            ctx.require("SELECT" not in encoded, "raw SQL leaked in default mode")
            ctx.require(
                span.get("sql_fingerprint"),
                f"missing SQL fingerprint: {span.get('name')}",
            )
            ctx.require(
                "input" not in span and "output" not in span,
                "content fields were sent in default mode",
            )
            ctx.require(
                span.get("table") == "shop.orders",
                f"table metadata missing: {span}",
            )
            ctx.require(
                span.get("is_error") is False,
                "tool status metadata missing in default mode",
            )
            if span.get("metadata", {}).get("duration_ms") is None:
                raise AssertionError("missing tool duration metadata")
            fingerprints.append(span["sql_fingerprint"])

        first, second, third = fingerprints
        ctx.require(
            first == second,
            "SQL fingerprints differ only because literals changed",
        )
        ctx.require(
            first != third,
            "SQL fingerprints do not distinguish different query structure",
        )

        _configure(ctx, receiver, "true")
        settings = ctx.rest.get("/api/v1/settings")
        ctx.require(settings.status_code == 200, settings.text)
        ctx.require(
            settings.json().get("langfuse_capture_content") == "true",
            "content capture switch was not persisted",
        )
        ctx.mcp_call("validate_sql", {"sql": sql_one, "dialect": "hive"})
        captured = receiver.wait_spans("validate_sql", 4)[-1]
        ctx.require(
            captured.get("input", {}).get("sql") == sql_one,
            "enabling content capture did not include the SQL input",
        )
        return {"fingerprints": fingerprints}
    finally:
        receiver.close()


def check_telemetry_survives_unavailable_receiver(ctx):
    response = ctx.rest.put(
        "/api/v1/settings",
        json={
            "values": {
                "langfuse_enabled": "true",
                "langfuse_url": "http://127.0.0.1:9",
                "langfuse_public_key": "issue-234-public",
                "langfuse_secret_key": LANGFUSE_SECRET,
            }
        },
    )
    ctx.require(response.status_code == 200, response.text)
    result = ctx.mcp_call("get_capabilities", {})
    ctx.require(isinstance(result, dict), "MCP call failed while telemetry was down")
    ctx.require(
        result.get("tools"), "MCP capabilities were empty while telemetry was down"
    )
    return {"tools": len(result.get("tools", []))}
