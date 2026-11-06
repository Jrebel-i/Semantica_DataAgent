"""Exercise a DTB pull over REST with an in-process fake DTB server."""

import io
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from openpyxl import Workbook


def check_dtb_pull(ctx):
    table_headers = [
        "database_name",
        "table_name",
        "column_name",
        "column_type",
        "comment",
    ]
    table_rows = [
        ["dimensions", "new_orders", "id", "string", "issue76 新订单"],
        ["dimensions", "z_orders", "id", "string", "issue76 历史订单"],
    ]

    def table_workbook(rows, limit):
        book = Workbook()
        book.active.append(table_headers)
        for row in rows[:limit]:
            book.active.append(row)
        output = io.BytesIO()
        book.save(output)
        return output.getvalue()

    tasks = json.dumps(
        {
            "columns": [{"name": "task_name"}, {"name": "task_src"}],
            "totalCount": 0,
            "data": [],
        }
    ).encode()
    state = {
        "failure": False,
        "truncate": False,
        "queries": {},
        "default_limits": {},
        "paths": [],
    }

    def last(key):
        return state["queries"][key][-1]

    def last_limit(key):
        return state["default_limits"][key][-1]

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
                "tables"
                if "ORDER BY table_name" in sql
                else "task"
                if "task_version" in sql
                else "stats"
                if "metadata_partition" in sql
                else "dimension"
                if "hive.dimensions" in sql
                else "table"
            )
            default_limit = request.get("defaultLimit")
            if (
                not isinstance(default_limit, int)
                or isinstance(default_limit, bool)
                or default_limit not in {100, 50000, 500000}
                or request.get("dataSource") != "doris_ai"
            ):
                self.send({}, status=400)
                return
            state["queries"].setdefault(key, []).append(sql)
            state["default_limits"].setdefault(key, []).append(default_limit)
            self.send({"status": "200", "data": {"queryKey": key}})

        def do_GET(self):
            state["paths"].append(self.path)
            parsed = urlsplit(self.path)
            if state["failure"]:
                self.send(
                    {
                        "status": "503",
                        "message": "未登录或token过期",
                        "data": None,
                    }
                )
            elif parsed.path == "/api/data-map/table":
                page = int(parse_qs(parsed.query).get("page", ["1"])[0])
                if page == 1:
                    tables = [
                        {
                            "tableId": identity,
                            "tableName": f"ods_{identity}",
                            "catalogType": "HIVE",
                        }
                        for identity in range(1, 501)
                    ]
                elif page == 2:
                    tables = [
                        {
                            "tableId": 501,
                            "tableName": "z_orders",
                            "catalogType": "HIVE",
                        },
                        {
                            "tableId": 502,
                            "tableName": "new_orders",
                            "catalogType": "HIVE",
                        },
                    ]
                else:
                    tables = []
                self.send(
                    {
                        "status": "200",
                        "data": {"tableList": tables, "total": 502},
                    }
                )
            elif parsed.path in {
                "/api/data-map/detail/501/column",
                "/api/data-map/detail/502/column",
            }:
                self.send(
                    {
                        "status": "200",
                        "data": {
                            "columnList": [
                                {"columnName": "id", "columnType": "string"},
                                {"columnName": "day_num", "columnType": "string"},
                            ],
                            "total": 2,
                        },
                    }
                )
            elif parsed.path in {
                "/api/data-map/detail/501",
                "/api/data-map/detail/502",
            }:
                identity = parsed.path.rsplit("/", 1)[-1]
                self.send(
                    {
                        "status": "200",
                        "data": {
                            "tableId": int(identity),
                            "tableName": "z_orders"
                            if identity == "501"
                            else "new_orders",
                            "databaseName": "dimensions",
                        },
                    }
                )
            elif self.path.startswith("/api/data-query/result/"):
                key = parsed.path.rsplit("/", 1)[-1]
                data = {"progress": 1.0}
                if state["truncate"]:
                    data["totalCount"] = last_limit(key)
                self.send({"status": "200", "data": data})
            elif self.path.startswith("/api/data-query/download/tables"):
                sql = last("tables")
                rows = [["501", "z_orders"], ["502", "new_orders"]]
                if "LIMIT 1" in sql:
                    rows = rows[1:]
                rows = rows[: last_limit("tables")]
                self.send(
                    {
                        "columns": [
                            {"name": "table_id", "type": "BIGINT"},
                            {"name": "table_name", "type": "VARCHAR"},
                        ],
                        "totalCount": len(rows),
                        "data": rows,
                    }
                )
            elif self.path.startswith("/api/data-query/download/task"):
                self.send(tasks)
            elif self.path.startswith("/api/data-query/download/stats"):
                self.send(
                    b"table_name,database_name,partition_name,records,rk\n"
                    b"z_orders,dimensions,day_num=20261105,1,1\n"
                    b"new_orders,dimensions,day_num=20261105,1,1\n",
                    "text/csv",
                )
            elif self.path.startswith("/api/data-query/download/dimension"):
                self.send(b"id,day_num\n1,20261105\n", "text/csv")
            elif self.path.startswith("/api/data-query/download/table"):
                sql = last("table")
                rows = table_rows
                if "a.table_name IN" in sql:
                    rows = [row for row in rows if f"'{row[1]}'" in sql]
                self.send(
                    table_workbook(rows, last_limit("table")),
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
        limits = state["default_limits"]
        ctx.require(
            set(queries) == {"tables", "task", "table", "stats", "dimension"},
            f"missing scoped queries: {list(queries)}",
        )
        table_list_sql = queries["tables"][-1]
        ctx.require(
            "database_name='dimensions'" in table_list_sql
            and "catalog_type='HIVE'" in table_list_sql
            and "status='在用'" in table_list_sql
            and "ORDER BY table_name LIMIT 1" in table_list_sql,
            "table list query is not scoped or bounded",
        )
        for key in ("task", "table", "stats"):
            ctx.require(
                all(
                    "new_orders" in sql and "ods_" not in sql and "z_orders" not in sql
                    for sql in queries[key]
                ),
                f"{key} query is not scoped to dimensions tables",
            )
        ctx.require(
            all(
                "ods_" not in sql and "z_orders" not in sql
                for sql in queries["dimension"]
            ),
            "dimension query uses a table outside the selected dimensions table",
        )
        ctx.require(
            "WHERE day_num = '20261105'" in queries["dimension"][-1]
            and "LIMIT 100" in queries["dimension"][-1],
            "dimension query must restrict latest partition and rows",
        )
        ctx.require(
            all(
                limit == 500000
                for key in ("tables", "task", "table", "stats")
                for limit in limits[key]
            )
            and limits["dimension"] == [100],
            f"scoped defaultLimit does not match query limits: {limits}",
        )
        ctx.require(
            not any("/detail/501" in path for path in state["paths"]),
            "unselected table was fetched",
        )
        ctx.require(
            not any("/api/data-map/table" in path for path in state["paths"]),
            "table list was paginated instead of read from metadata SQL",
        )

        def wait_build(record):
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                result = ctx.rest.get(f"/api/v1/builds/{record['build_id']}")
                result.raise_for_status()
                build = result.json()
                if build["status"] in ("succeeded", "failed", "cancelled"):
                    return build
                time.sleep(0.5)
            raise AssertionError("incremental build timed out")

        build = wait_build(record)
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

        state["truncate"] = True
        truncated = ctx.rest.post("/api/v1/pulls/dtb").json()
        ctx.require(
            truncated["status"] == "failed"
            and "DTB result truncated" in truncated["error"],
            f"truncated result was not rejected: {truncated}",
        )
        ctx.require(
            git("rev-parse", "HEAD") == record["commit"],
            "truncated pull changed source commit",
        )
        state["truncate"] = False
        state["failure"] = True
        failed = ctx.rest.post("/api/v1/pulls/dtb").json()
        ctx.require(
            failed["status"] == "failed"
            and "authentication" in failed["error"]
            and "503" in failed["error"],
            f"authentication failure not recorded: {failed}",
        )
        ctx.require(
            git("rev-parse", "HEAD") == record["commit"],
            "failed pull changed source commit",
        )
        listing = ctx.rest.get("/api/v1/pulls").json()
        ctx.require(
            len(listing) >= 3 and listing[0]["status"] == "failed",
            "pull history missing",
        )
        return {"commit": record["commit"], "build": record["build_id"]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
