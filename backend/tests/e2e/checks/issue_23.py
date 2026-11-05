"""End-to-end Langfuse checks against the isolated test project."""

import hashlib
import json
import os
import subprocess
import time
import uuid

import httpx

# Independent contract: changing the implementation allowlist must not change this check.
ALLOWED_FIELDS = frozenset(
    {
        "id",
        "traceId",
        "parentObservationId",
        "name",
        "input",
        "output",
        "metadata",
        "agent",
        "provider",
        "model",
        "source",
        "token_name",
        "tool",
        "arguments",
        "result",
        "duration_ms",
        "status",
        "sql",
        "dialect",
        "query",
        "table",
        "tables",
        "database",
        "text",
        "summary",
        "limit",
        "direction",
        "depth",
        "document",
        "category",
        "ref",
        "type",
        "is_error",
        "started_at",
        "ended_at",
        "startTime",
        "endTime",
        "valid",
        "errors",
        "warnings",
        "names",
        "scope",
        "top_k",
        "keyword",
        "field",
        "doc_id",
        "section",
        "task_id",
        "questions",
        "question",
        "options",
        "label",
        "conversation_id",
    }
)

LANGFUSE = "http://172.16.242.124:31631"


def _key(field):
    command = f"kubectl -n semantica-dataagent-langfuse get secret langfuse-test-keys -o jsonpath='{{.data.{field}}}' | base64 -d"
    return subprocess.check_output(
        ["ssh", "-o", "BatchMode=yes", "baize@172.16.242.124", command], text=True
    ).strip()


def _ask(ctx, provider, question):
    response = ctx.rest.post(
        "/api/v1/conversations/ask",
        json={
            "question": question,
            "agent": "pi",
            "provider_id": provider,
            "model": os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude"),
        },
        timeout=300,
    )
    ctx.require(response.status_code == 200, f"ask HTTP {response.status_code}")
    events = []
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if event and data:
            events.append((event, json.loads(data)))
    ctx.require(
        events and events[-1][0] == "done", f"answer failed: {[e[0] for e in events]}"
    )
    info = events[0][1]
    trace_id = hashlib.sha256(
        f"{info['conversation_id']}:{info['turn_id']}".encode()
    ).hexdigest()[:32]
    return trace_id, events


def _wait_trace(client, trace_id, predicate, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        response = client.get(f"/api/public/traces/{trace_id}")
        if response.status_code == 200:
            trace = response.json()
            if predicate(trace):
                return trace
        time.sleep(1)
    raise AssertionError(f"trace {trace_id} did not reach expected state")


def check_langfuse_observability(ctx):
    public, secret = _key("public-key"), _key("secret-key")
    token_name = "issue23-" + uuid.uuid4().hex[:12]
    ids = []
    with httpx.Client(base_url=LANGFUSE, auth=(public, secret), timeout=10) as langfuse:
        try:
            response = ctx.rest.put(
                "/api/v1/settings",
                json={
                    "values": {
                        "langfuse_enabled": "true",
                        "langfuse_url": LANGFUSE,
                        "langfuse_public_key": public,
                        "langfuse_secret_key": secret,
                    }
                },
            )
            ctx.require(
                response.status_code == 200 and secret not in response.text,
                "could not configure Langfuse privately",
            )
            provider = ctx.rest.post(
                "/api/v1/providers",
                json={
                    "name": "Issue 23",
                    "type": "anthropic-messages",
                    "base_url": os.environ.get(
                        "TEST_PI_BASE_URL", "http://127.0.0.1:3425"
                    ),
                    "models": [
                        os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")
                    ],
                },
            ).json()["id"]
            marker = "issue23bearer" + uuid.uuid4().hex
            question = (
                "查询 shop.payments 表，先调用 search_knowledge，再写按 dt 统计记录数的 Hive SQL，并调用 validate_sql 验证后回答。Bearer "
                + marker
            )
            online_id, _events = _ask(ctx, provider, question)
            ids.append(online_id)
            trace = _wait_trace(
                langfuse,
                online_id,
                lambda t: (
                    bool(t.get("output"))
                    and any(
                        o.get("name") == "validate_sql"
                        for o in t.get("observations", [])
                    )
                ),
            )
            ctx.require(
                marker not in json.dumps(trace, ensure_ascii=False),
                "Bearer secret leaked",
            )
            ctx.require(
                "Bearer [REDACTED]" in str(trace.get("input")),
                "question credential was not redacted",
            )
            names = [o.get("name") for o in trace.get("observations", [])]
            ctx.require(
                "search_knowledge" in names and "validate_sql" in names,
                f"missing MCP observations: {names}",
            )
            ctx.require(
                trace.get("metadata", {}).get("agent") == "pi", "missing agent metadata"
            )
            issued = ctx.rest.post(
                "/api/v1/tokens", json={"name": token_name, "role": "user"}
            ).json()
            local = ctx.rest.post(
                "/mcp/",
                headers={
                    "Authorization": "Bearer " + issued["token"],
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "validate_sql",
                        "arguments": {
                            "sql": "SELECT 1 -- " + issued["token"],
                            "dialect": "hive",
                            "unexpected_issue23": "private-extra-field",
                        },
                    },
                },
            )
            ctx.require(local.status_code == 200, f"local MCP HTTP {local.status_code}")
            deadline = time.monotonic() + 90
            local_trace = None
            while time.monotonic() < deadline:
                listing = langfuse.get(
                    "/api/public/traces", params={"name": "local-mcp", "limit": 100}
                ).json()
                local_trace = next(
                    (
                        t
                        for t in listing.get("data", [])
                        if t.get("metadata", {}).get("token_name") == token_name
                    ),
                    None,
                )
                if local_trace:
                    break
                time.sleep(1)
            ctx.require(local_trace is not None, "local MCP trace missing token name")
            ids.append(local_trace["id"])
            local_trace = _wait_trace(
                langfuse, local_trace["id"], lambda t: bool(t.get("observations"))
            )
            validation = next(
                o for o in trace["observations"] if o.get("name") == "validate_sql"
            )
            ctx.require(
                validation.get("input", {}).get("sql"), "validation SQL missing"
            )
            ctx.require(
                validation.get("output", {}).get("status"), "validation result missing"
            )
            ctx.require(
                validation.get("metadata", {}).get("duration_ms") is not None,
                "tool duration missing",
            )

            def check_fields(value):
                if isinstance(value, dict):
                    ctx.require(
                        set(value) <= ALLOWED_FIELDS,
                        f"unexpected telemetry fields: {set(value) - ALLOWED_FIELDS}",
                    )
                    for child in value.values():
                        check_fields(child)
                elif isinstance(value, list):
                    for child in value:
                        check_fields(child)

            ctx.require(
                "private-extra-field" not in json.dumps(local_trace),
                "extra field leaked",
            )
            for item in (trace, local_trace):
                for key in ("input", "output", "metadata"):
                    check_fields(item.get(key))
                for observation in item.get("observations", []):
                    for key in ("input", "output", "metadata"):
                        check_fields(observation.get(key))
                serialized = json.dumps(item, ensure_ascii=False)
                for forbidden in (ctx.token, issued["token"], secret):
                    ctx.require(
                        forbidden not in serialized, "credential leaked into trace"
                    )
                allowed = {
                    "agent",
                    "provider",
                    "model",
                    "source",
                    "token_name",
                    "duration_ms",
                }
                ctx.require(
                    set(item.get("metadata") or {}) <= allowed,
                    "unexpected metadata field",
                )
            ctx.rest.put(
                "/api/v1/settings", json={"values": {"langfuse_enabled": "false"}}
            )
            disabled_id, disabled_events = _ask(ctx, provider, question)
            ctx.require(disabled_events[-1][0] == "done", "disabled answer failed")
            time.sleep(5)
            ctx.require(
                langfuse.get(f"/api/public/traces/{disabled_id}").status_code == 404,
                "disabled turn produced a trace",
            )
            return {
                "online_trace": online_id,
                "local_trace": local_trace["id"],
                "tools": names,
            }
        finally:
            ctx.rest.put(
                "/api/v1/settings", json={"values": {"langfuse_enabled": "false"}}
            )
            for trace_id in ids:
                langfuse.delete(f"/api/public/traces/{trace_id}")
