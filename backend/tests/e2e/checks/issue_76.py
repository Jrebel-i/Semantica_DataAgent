"""Exercise a DTB pull over REST with an in-process fake DTB server."""

import io
import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from openpyxl import Workbook

METADATA_COLUMNS = [
    "table_id",
    "table_name",
    "database_name",
    "create_time",
    "modify_time",
    "comment",
    "output_task_id",
    "output_task_name",
    "use_direction",
    "column_name",
    "seq_no",
    "column_type",
    "column_comment",
    "column_business_comment",
]
PARTITION_HEADER = (
    "table_id,table_name,database_name,create_time,modify_time,comment,"
    "partition_id,partition_name,records,partition_create_time,rk"
)


def check_dtb_pull(ctx):
    state = {
        "failure": False,
        "multiple_partition": set(),
        "queries": {},
        "default_limits": {},
        "paths": [],
    }

    def metadata_rows():
        rows = []
        for table_id, name in ((501, "new_orders"), (502, "z_orders")):
            rows.append(
                [
                    str(table_id),
                    name,
                    "dimensions",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "id",
                    1,
                    "string",
                    "编号",
                    "业务编号",
                ]
            )
            rows.append(
                [
                    str(table_id),
                    name,
                    "dimensions",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "day_num",
                    10001,
                    "string",
                    "分区",
                    None,
                ]
            )
            if name in state["multiple_partition"]:
                rows.append(
                    [
                        str(table_id),
                        name,
                        "dimensions",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "hour_num",
                        10002,
                        "string",
                        "二级分区",
                        None,
                    ]
                )
        return rows

    def metadata_payload(rows, limit):
        selected = rows[:limit] if limit else rows
        return json.dumps(
            {
                "columns": [
                    {"name": name, "type": "VARCHAR"} for name in METADATA_COLUMNS
                ],
                "data": selected,
            }
        ).encode()

    def partition_row(table_id, name):
        return ",".join(
            [
                str(table_id),
                name,
                "dimensions",
                "",
                "",
                "",
                "",
                "day_num=20261105",
                "1",
                "",
                "1",
            ]
        )

    def stats_csv(limit):
        lines = [
            PARTITION_HEADER,
            partition_row(501, "new_orders"),
            partition_row(502, "z_orders"),
        ]
        if limit:
            lines = lines[: limit + 1]
        return ("\n".join(lines) + "\n").encode()

    table_rows = [
        ["dimensions", "new_orders", "id", "string"],
        ["dimensions", "z_orders", "id", "string"],
    ]

    def table_workbook(sql, limit):
        rows = table_rows
        match = re.search(r"a\.table_name IN \(([^)]*)\)", sql)
        if match:
            allowed = set(re.findall(r"'([^']+)'", match.group(1)))
            rows = [row for row in rows if row[1] in allowed]
        if limit:
            rows = rows[:limit]
        book = Workbook()
        book.active.append(
            ["database_name", "table_name", "column_name", "column_type"]
        )
        for row in rows:
            book.active.append(row)
        output = io.BytesIO()
        book.save(output)
        return output.getvalue()

    def key_for(sql):
        if "ORDER BY a.table_id" in sql:
            return "metadata"
        if "task_version" in sql:
            return "task"
        if "metadata_partition" in sql:
            return "stats"
        match = re.search(r"hive\.dimensions\.([A-Za-z_][A-Za-z0-9_]*)", sql)
        if match:
            return "dim_" + match.group(1)
        return "table"

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
            key = key_for(sql)
            limit = request.get("defaultLimit")
            allowed = (
                limit in {0}
                if key in {"metadata", "task", "table", "stats"}
                else limit in {100, 50000}
                if key.startswith("dim_")
                else False
            )
            if not allowed or request.get("dataSource") != "doris_ai":
                self.send({}, status=400)
                return
            state["queries"].setdefault(key, []).append(sql)
            state["default_limits"].setdefault(key, []).append(limit)
            if state["failure"]:
                self.send(
                    {
                        "status": "503",
                        "message": "未登录或token过期",
                        "data": None,
                    }
                )
                return
            self.send({"status": "200", "data": {"queryKey": key}})

        def do_GET(self):
            state["paths"].append(self.path)
            parsed = urlsplit(self.path)
            if parsed.path.startswith("/api/data-map"):
                self.send({}, status=404)
            elif parsed.path.startswith("/api/data-query/result/"):
                self.send({"status": "200", "data": {"progress": 1.0}})
            elif parsed.path.startswith("/api/data-query/download/"):
                key = parsed.path.rsplit("/", 1)[-1]
                limit = state["default_limits"][key][-1]
                if key == "metadata":
                    self.send(metadata_payload(metadata_rows(), limit))
                elif key == "task":
                    self.send(
                        {
                            "columns": [
                                {"name": "task_name"},
                                {"name": "task_src"},
                            ],
                            "data": [],
                        }
                    )
                elif key == "stats":
                    self.send(stats_csv(limit), "text/csv")
                elif key.startswith("dim_"):
                    rows = [["id", "day_num"], ["1", "20261105"]]
                    if limit:
                        rows = rows[: limit + 1]
                    self.send(
                        ("\n".join(",".join(row) for row in rows) + "\n").encode(),
                        "text/csv",
                    )
                elif key == "table":
                    sql = state["queries"]["table"][-1]
                    self.send(
                        table_workbook(sql, limit),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                else:
                    self.send({}, status=404)
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
        base_parameters = {
            "url": f"http://127.0.0.1:{server.server_port}",
            "token": "fake-dtb-token",
            "interval_seconds": 0,
            "layer": "dimensions",
        }

        def configure(parameters):
            response = ctx.rest.put(
                "/api/v1/plugins/puller/dtb",
                json={"enabled": True, "parameters": parameters},
            )
            response.raise_for_status()
            ctx.require(
                "fake-dtb-token" not in response.text, "pull token was returned"
            )

        def pull():
            response = ctx.rest.post("/api/v1/pulls/dtb")
            response.raise_for_status()
            return response.json()

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

        def commit_files(commit):
            return git("show", "--pretty=format:", "--name-only", commit).splitlines()

        def snapshot_file(commit, suffix):
            tree = commit_files(commit)
            path = next(path for path in tree if path.endswith(suffix))
            return json.loads(git("show", f"{commit}:{path}"))

        configure({**base_parameters, "max_tables": 1})
        record = pull()
        ctx.require(record["status"] == "succeeded", f"pull failed: {record}")
        ctx.require(
            record["error"] is None, f"bounded pull reported failures: {record}"
        )
        ctx.require(
            git("rev-parse", "HEAD") == record["commit"] != before,
            "pull did not commit source",
        )
        ctx.require(record["tables"] == 1, "pull did not record selected table")
        queries = state["queries"]
        limits = state["default_limits"]
        ctx.require(
            set(queries) == {"metadata", "task", "table", "stats", "dim_new_orders"},
            f"missing scoped queries: {list(queries)}",
        )
        metadata_sql = queries["metadata"][-1]
        ctx.require(
            "metadata_column" in metadata_sql
            and "ORDER BY a.table_id, b.seq_no" in metadata_sql
            and "LIMIT" not in metadata_sql,
            "dimensions metadata is not the fixed unbounded SQL",
        )
        for key in ("task", "table", "stats"):
            ctx.require(
                all(
                    "new_orders" in sql and "ods_" not in sql and "z_orders" not in sql
                    for sql in queries[key]
                ),
                f"{key} query is not scoped to the selected dimensions table",
            )
        ctx.require(
            "output_table_name IN ('new_orders')" in queries["task"][-1]
            and "a.table_name IN ('new_orders')" in queries["table"][-1]
            and "table_name IN ('new_orders')" in queries["stats"][-1],
            "bounded pull did not filter metadata by the selected table",
        )
        ctx.require(
            "WHERE day_num = '20261105' LIMIT 100" in queries["dim_new_orders"][-1],
            "dimension query must restrict the latest partition and row count",
        )
        ctx.require(
            all(limits[key] == [0] for key in ("metadata", "task", "table", "stats"))
            and limits["dim_new_orders"] == [100],
            f"defaultLimit does not match the DataAgent contract: {limits}",
        )
        ctx.require(
            not any(path.startswith("/api/data-map") for path in state["paths"]),
            "the puller still called a data-map endpoint",
        )
        meta = snapshot_file(record["commit"], "dimensions/new_orders/meta.json")
        ctx.require(
            meta["partition_field"] == "day_num"
            and meta["latest_partition"] == "20261105"
            and [column["name"] for column in meta["columns"]] == ["id", "day_num"]
            and meta["columns"][1]["seq_no"] == 10001,
            f"dimension metadata is not the SQL metadata: {meta}",
        )
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

        state["queries"].clear()
        state["default_limits"].clear()
        state["paths"].clear()
        configure(base_parameters)
        full = pull()
        ctx.require(full["status"] == "succeeded", f"full pull failed: {full}")
        ctx.require(full["error"] is None, f"full pull reported failures: {full}")
        ctx.require(
            full["tables"] == 2, "full pull did not cover all dimensions tables"
        )
        ctx.require(
            git("rev-parse", "HEAD") == full["commit"] != record["commit"],
            "full pull did not commit source",
        )
        queries = state["queries"]
        limits = state["default_limits"]
        ctx.require(
            "LIMIT" not in queries["metadata"][-1],
            "full dimensions metadata must not be bounded",
        )
        ctx.require(
            "output_table_name IN" not in queries["task"][-1],
            "full task query must not filter by dimensions tables",
        )
        ctx.require(
            "a.table_name IN" not in queries["table"][-1]
            and "database_name='dimensions'" not in queries["table"][-1],
            "full table query must not filter by dimensions tables",
        )
        ctx.require(
            "table_name IN" not in queries["stats"][-1]
            and "database_name='dimensions'" in queries["stats"][-1],
            "full partition query must cover every dimensions table",
        )
        ctx.require(
            all(limits[key] == [0] for key in ("metadata", "task", "table", "stats"))
            and limits["dim_new_orders"] == [50000]
            and limits["dim_z_orders"] == [50000],
            f"full defaultLimit does not match the contract: {limits}",
        )
        build = wait_build(full)
        ctx.require(build["status"] == "succeeded", f"full build failed: {build}")

        state["multiple_partition"] = {"z_orders"}
        partial = pull()
        ctx.require(
            partial["status"] == "succeeded",
            f"partial pull was not isolated: {partial}",
        )
        ctx.require(
            "z_orders" in (partial["error"] or "")
            and "multiple_partition_fields" in (partial["error"] or ""),
            f"partial failure was not recorded: {partial}",
        )
        ctx.require(
            git("rev-parse", "HEAD") == partial["commit"] != full["commit"],
            "partial pull did not commit the healthy tables",
        )
        tree = commit_files(partial["commit"])
        ctx.require(
            not any("dimensions/z_orders/" in path for path in tree),
            "failed table was written to the snapshot",
        )
        ctx.require(
            any("dimensions/new_orders/meta.json" in path for path in tree),
            "healthy table was not written to the snapshot",
        )

        state["multiple_partition"] = {"new_orders", "z_orders"}
        failed = pull()
        ctx.require(
            failed["status"] == "failed"
            and "All DTB dimension tables failed" in (failed["error"] or ""),
            f"all-table failure was not fatal: {failed}",
        )
        ctx.require(
            git("rev-parse", "HEAD") == partial["commit"],
            "failed pull changed the source commit",
        )

        state["failure"] = True
        unauthorized = pull()
        ctx.require(
            unauthorized["status"] == "failed"
            and "authentication" in (unauthorized["error"] or "")
            and "503" in (unauthorized["error"] or ""),
            f"authentication failure was not recorded: {unauthorized}",
        )
        ctx.require(
            git("rev-parse", "HEAD") == partial["commit"],
            "authentication failure changed the source commit",
        )
        listing = ctx.rest.get("/api/v1/pulls").json()
        ctx.require(
            len(listing) >= 5 and listing[0]["status"] == "failed",
            "pull history missing",
        )
        return {"commit": partial["commit"], "build": full["build_id"]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
