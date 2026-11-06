"""Exercise confirmed online queries with a fake DTB and the real model gateway."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def screenshot(ctx, result=False):
    if not os.environ.get("ISSUE81_SCREENSHOTS"):
        return
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(ctx.runtime.base_url, wait_until="domcontentloaded")
            page.evaluate("token => localStorage.setItem('token', token)", ctx.token)
            page.goto(ctx.runtime.base_url + "/chat", wait_until="networkidle")
            page.locator(
                ".query-result-preview" if result else ".query-plan-card"
            ).wait_for(timeout=15000)
            if result:
                ctx.require(
                    page.get_by_role("tab", name="结果").get_attribute("aria-selected")
                    == "true",
                    "result panel did not follow the completed query",
                )
            output = Path(os.environ["ISSUE81_SCREENSHOTS"])
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(output / ("result-1440.png" if result else "plan-1440.png"))
            )
        finally:
            browser.close()


def events(body):
    found = []
    for block in body.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            if line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            found.append((name, data))
    return found


def check_online_query_confirmation(ctx):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request)
            payload = {"status": 200, "data": {"queryKey": "query-81"}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            if "/result/" in self.path:
                data = {
                    "progress": 1,
                    "columns": [{"name": "sales", "type": "long"}],
                    "data": [[12345]],
                }
                payload = json.dumps({"status": 200, "data": data}).encode()
            else:
                payload = b"sales\n12345\n"
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
                    "row_limit": 100,
                    "download_limit": 100,
                    "timeout_seconds": 5,
                },
            },
        )
        ctx.require(response.status_code == 200, response.text)
        ctx.require(
            ctx.rest.put(
                "/api/v1/me/dtb-token", json={"token": "fake-token"}
            ).status_code
            == 200,
            "token setup failed",
        )
        model = os.environ.get("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")
        provider = ctx.rest.post(
            "/api/v1/providers",
            json={
                "name": "issue 81 e2e",
                "type": "anthropic-messages",
                "base_url": os.environ.get("TEST_PI_BASE_URL", "http://127.0.0.1:3425"),
                "api_key": "test",
                "models": [model],
            },
        )
        ctx.require(provider.status_code == 201, provider.text)
        with ctx.rest.stream(
            "POST",
            "/api/v1/conversations/ask",
            json={
                "question": "计算 shop.orders 在 dt='2025-01-01' 的订单金额合计，按 amount 求和即可。请先查表结构，用 Doris SQL 校验后调用 propose_query 提议执行，等待确认。",
                "agent": "pi",
                "provider_id": provider.json()["id"],
                "model": model,
            },
            timeout=600,
        ) as response:
            ctx.require(response.status_code == 200, str(response.status_code))
            initial = events("".join(response.iter_text()))
        plan_events = [data for name, data in initial if name == "query_plan"]
        ctx.require(
            len(plan_events) == 1,
            f"missing query plan; tool events: {[(name, data) for name, data in initial if name in ('tool_start', 'tool_end')]}",
        )

        def executions():
            return [call for call in calls if "_dataagent_query" in call.get("sql", "")]

        ctx.require(not executions(), f"query ran before confirmation: {calls}")
        screenshot(ctx)
        info = next(data for name, data in initial if name == "conversation")
        plan = plan_events[0]
        path = f"/api/v1/conversations/{info['conversation_id']}/turns/{info['turn_id']}/queries/{plan['id']}/execute"
        direct = ctx.rest.post(f"/api/v1/queries/{plan['id']}/execute", json={})
        ctx.require(
            direct.status_code == 403 and not executions(),
            "online query bypassed conversation confirmation",
        )
        other = ctx.rest.post(
            "/api/v1/tokens", json={"name": "issue-81-other", "role": "user"}
        )
        ctx.require(other.status_code == 201, other.text)
        rejected = ctx.rest.post(
            path, headers={"Authorization": "Bearer " + other.json()["token"]}
        )
        ctx.require(
            rejected.status_code == 404 and not executions(),
            f"other user confirmation accepted: {rejected.status_code}",
        )
        with ctx.rest.stream("POST", path, timeout=600) as response:
            ctx.require(response.status_code == 200, str(response.status_code))
            finished = events("".join(response.iter_text()))
        result = next((data for name, data in finished if name == "query_result"), None)
        ctx.require(
            result and result["columns"] == ["sales"] and result["rows"] == [[12345]],
            f"incorrect result: {result}",
        )
        ctx.require(
            len(executions()) == 1 and executions()[0]["dataSource"] == "doris_ai",
            f"incorrect execution count: {calls}",
        )
        done = next((data for name, data in finished if name == "done"), None)
        ctx.require(
            done and done.get("text"), f"agent conclusion missing: {finished[-3:]}"
        )
        ctx.require(
            ctx.rest.post(path).status_code == 409, "confirmation could be replayed"
        )
        detail = ctx.rest.get(f"/api/v1/conversations/{info['conversation_id']}")
        ctx.require(
            detail.status_code == 200
            and any(
                e["type"] == "query_result" for e in detail.json()["turns"][0]["events"]
            ),
            "result was not saved",
        )
        ctx.require(
            ctx.rest.get(f"/api/v1/queries/{plan['id']}/download?format=csv").content
            == b"sales\n12345\n",
            "download failed",
        )
        screenshot(ctx, result=True)
        return {
            "conversation_id": info["conversation_id"],
            "rows": result["rows"],
            "conclusion": done["text"],
        }
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
