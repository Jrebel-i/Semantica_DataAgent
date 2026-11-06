"""Exercise a DTB pull over REST with an in-process fake DTB server."""

import io
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openpyxl import Workbook


def check_dtb_pull(ctx):
    book = Workbook()
    book.active.append(
        ["database_name", "table_name", "column_name", "column_type", "comment"]
    )
    book.active.append(["dimensions", "new_orders", "id", "string", "issue76 新订单"])
    output = io.BytesIO()
    book.save(output)
    tasks = json.dumps(
        {"columns": [{"name": "task_name"}, {"name": "task_src"}], "data": []}
    ).encode()
    state = {"failure": False, "queries": {}, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, data, content_type="application/json", status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(
                data if isinstance(data, bytes) else json.dumps(data).encode()
            )

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(size))
            sql = request["sql"]
            key = (
                "task"
                if "task_version" in sql
                else "stats"
                if "metadata_partition" in sql
                else "dimension"
                if "hive.dimensions" in sql
                else "table"
            )
            state["queries"][key] = sql
            if (
                request.get("defaultLimit") != 100
                or request.get("dataSource") != "doris_ai"
            ):
                self.send({}, status=400)
                return
            self.send({"status": "200", "data": {"queryKey": key}})

        def do_GET(self):
            state["paths"].append(self.path)
            if state["failure"]:
                self.send({}, status=401)
            elif self.path.startswith("/api/data-map/table"):
                self.send(
                    {
                        "status": "200",
                        "data": {
                            "tableList": [
                                {
                                    "tableId": 1,
                                    "tableName": "new_orders",
                                    "catalogType": "HIVE",
                                }
                            ],
                            "totalElements": 2,
                        },
                    }
                )
            elif self.path.startswith("/api/data-map/detail/1/column"):
                self.send(
                    {
                        "status": "200",
                        "data": {
                            "columnList": [
                                {"columnName": "id", "columnType": "string"},
                                {"columnName": "day_num", "columnType": "string"},
                            ],
                            "totalElements": 2,
                        },
                    }
                )
            elif self.path == "/api/data-map/detail/1":
                self.send(
                    {"status": "200", "data": {"tableId": 1, "tableName": "new_orders"}}
                )
            elif self.path.startswith("/api/data-query/result/"):
                self.send({"status": "200", "data": {"progress": 1.0}})
            elif self.path.startswith("/api/data-query/download/task"):
                self.send(tasks)
            elif self.path.startswith("/api/data-query/download/stats"):
                self.send(
                    b"table_name,database_name,partition_name,records,rk\nnew_orders,dimensions,day_num=20261105,1,1\n",
                    "text/csv",
                )
            elif self.path.startswith("/api/data-query/download/dimension"):
                self.send(b"id,day_num\n1,20261105\n", "text/csv")
            elif self.path.startswith("/api/data-query/download/table"):
                self.send(
                    output.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            else:
                self.send({}, status=404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        git = lambda *args: (
            subprocess.check_output(["git", "-C", str(ctx.source_dir), *args])
            .decode()
            .strip()
        )
        before = git("rev-parse", "HEAD")
        response = ctx.rest.put(
            "/api/v1/plugins/puller/dtb",
            json={
                "enabled": True,
                "parameters": {
                    "url": f"http://127.0.0.1:{server.server_port}",
                    "token": "fake-dtb-token",
                    "interval_seconds": 0,
                    "layer": "dimensions",
                    "max_tables": 1,
                },
            },
        )
        response.raise_for_status()
        ctx.require("fake-dtb-token" not in response.text, "pull token was returned")
        response = ctx.rest.post("/api/v1/pulls/dtb")
        response.raise_for_status()
        record = response.json()
        ctx.require(record["status"] == "succeeded", f"pull failed: {record}")
        ctx.require(
            git("rev-parse", "HEAD") == record["commit"] != before,
            "pull did not commit source",
        )
        ctx.require(record["tables"] == 1, "pull did not record selected table")
        queries = state["queries"]
        ctx.require(
            set(queries) == {"task", "table", "stats", "dimension"},
            f"missing scoped queries: {list(queries)}",
        )
        for key in ("task", "table", "stats"):
            ctx.require("new_orders" in queries[key], f"{key} query is not scoped")
        ctx.require(
            "WHERE day_num = '20261105'" in queries["dimension"]
            and "LIMIT 100" in queries["dimension"],
            "dimension query must restrict latest partition and rows",
        )
        ctx.require(
            not any("/detail/2" in path for path in state["paths"]),
            "unselected table was fetched",
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = ctx.rest.get(f"/api/v1/builds/{record['build_id']}")
            result.raise_for_status()
            build = result.json()
            if build["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.5)
        ctx.require(
            build["status"] == "succeeded", f"incremental build failed: {build}"
        )
        result = ctx.rest.get(
            "/api/v1/search",
            params={"query": "new_orders", "scope": "tables", "top_k": 10},
        )
        result.raise_for_status()
        ctx.require(
            "new_orders" in result.text, f"new table absent from search: {result.text}"
        )
        state["failure"] = True
        failed = ctx.rest.post("/api/v1/pulls/dtb").json()
        ctx.require(
            failed["status"] == "failed" and "401" in failed["error"],
            f"401 not recorded: {failed}",
        )
        ctx.require(
            git("rev-parse", "HEAD") == record["commit"],
            "failed pull changed source commit",
        )
        listing = ctx.rest.get("/api/v1/pulls").json()
        ctx.require(
            len(listing) >= 2 and listing[0]["status"] == "failed",
            "pull history missing",
        )
        return {"commit": record["commit"], "build": record["build_id"]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
