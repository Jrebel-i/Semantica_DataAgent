"""Run the execution evaluator against the application and a fake DTB."""

import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

import yaml


def check_execution_evaluation(ctx):
    from semantica_dataagent.eval_exec import run_cli

    queries = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = str(len(queries))
            queries[key] = body["sql"]
            payload = {"status": 200, "data": {"queryKey": key}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            sql = queries[self.path.rsplit("/", 1)[-1]]
            rows = [[1], [2]] if "amount" in sql else [[1]]
            data = {
                "progress": 1,
                "columns": [{"name": "value", "type": "long"}],
                "data": rows,
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": 200, "data": data}).encode())

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
                    "row_limit": 1,
                    "download_limit": 10,
                    "timeout_seconds": 5,
                },
            },
        )
        ctx.require(response.status_code == 200, response.text)
        ctx.require(
            ctx.rest.put(
                "/api/v1/me/dtb-token", json={"token": "issue85-fake-dtb-token"}
            ).status_code
            == 200,
            "token setup failed",
        )
        base = "SELECT id FROM shop.orders WHERE dt = '2025-01-01'"
        amount = "SELECT amount FROM shop.orders WHERE dt = '2025-01-01'"
        questions = [
            {
                "id": f"r{i}",
                "question": f"question {i}",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["id"],
                "caliber": "test",
                "reference_sql": base,
            }
            for i in range(4)
        ]
        answers = {
            "r0": base,
            "r1": amount,
            "r2": "DELETE FROM shop.orders",
            "r3": None,
        }
        with tempfile.TemporaryDirectory(prefix="issue85-") as directory:
            path = Path(directory)
            (path / "questions.yaml").write_text(
                yaml.safe_dump(questions), encoding="utf-8"
            )
            args = SimpleNamespace(
                questions=path / "questions.yaml",
                report=path / "report",
                base_url=ctx.runtime.base_url,
                token="ISSUE85_APP_TOKEN",
                only=None,
                max_queries=40,
                skip_reference=False,
                agent="pi",
                provider_id="fake",
                model="fake",
            )
            with (
                patch.dict(os.environ, {"ISSUE85_APP_TOKEN": ctx.token}),
                patch(
                    "semantica_dataagent.eval_exec._answer",
                    side_effect=lambda client, question, args: answers[question.id],
                ),
            ):
                ctx.require(run_cli(args) == 0, "evaluation failed")
            raw = (path / "report.json").read_text()
            report = json.loads(raw)
            metrics = report["metrics"]
            ctx.require(
                metrics["execution_success"] == 2 and metrics["execution_total"] == 3,
                str(metrics),
            )
            ctx.require(
                metrics["equal"] == 1 and metrics["comparable"] == 1, str(metrics)
            )
            ctx.require(
                metrics["no_sql"] == 1 and metrics["reference_failures"] == 0,
                str(metrics),
            )
            ctx.require(
                report["questions"][1]["comparison"] == "uncomparable", str(report)
            )
            ctx.require(
                report["questions"][2]["agent"]["reason"] == "校验被拒", str(report)
            )
            ctx.require(
                ctx.token not in raw
                and ctx.token not in (path / "report.md").read_text(),
                "token leaked",
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
