"""Exercise system-generated sampling through MCP with a fake DTB endpoint."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread


def check_sample_table_data(ctx):
    calls = []
    queries = {}

    class DtbHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            token = self.headers.get("token")
            calls.append((token, body))
            key = "sample-" + str(len(calls))
            queries[key] = body.get("sql", "")
            if token == "expired":
                self.send_response(401)
                self.end_headers()
                return
            payload = json.dumps({"status": 200, "data": {"queryKey": key}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            key = self.path.rstrip("/").rsplit("/", 1)[-1]
            sql = queries.get(key, "")
            if "metadata_table" in sql:
                if "sales_fact" in sql:
                    data = {
                        "progress": 1.0,
                        "columns": [{"name": "partition_name", "type": "char"}],
                        "data": [["dt=2026-08-31"]],
                    }
                else:
                    data = {
                        "progress": 1.0,
                        "columns": [{"name": "partition_name", "type": "char"}],
                        "data": [],
                    }
            else:
                data = {
                    "progress": 1.0,
                    "columns": [
                        {"name": "id", "type": "long"},
                        {"name": "dt", "type": "date"},
                    ],
                    "data": [[1, "2025-01-03"], [2, "2025-01-03"]],
                }
            payload = json.dumps({"status": 200, "data": data}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), DtbHandler)
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
                    "timeout_seconds": 2,
                },
            },
        )
        ctx.require(response.status_code == 200, response.text)

        missing = ctx.mcp_call(
            "sample_table_data", {"table": "shop.orders"}, allow_error=True
        )
        ctx.require(missing.isError, "sampling without a DTB token succeeded")
        ctx.require(
            missing.structuredContent["code"] == "invalid_input",
            f"unexpected missing-token code: {missing.structuredContent}",
        )
        ctx.require(
            "DTB token" in missing.structuredContent["message"],
            "missing-token error is not explicit",
        )

        response = ctx.rest.put("/api/v1/me/dtb-token", json={"token": "caller-token"})
        ctx.require(response.status_code == 200, response.text)
        ctx.require("caller-token" not in response.text, "DTB token was echoed")

        sample = ctx.mcp_call(
            "sample_table_data",
            {"table": "shop.orders", "columns": ["id", "dt"], "limit": 2},
        )
        ctx.require(sample["rows"] == [[1, "2025-01-03"], [2, "2025-01-03"]], sample)
        ctx.require(sample["row_count"] == 2, sample)
        ctx.require(
            sample["partition"] == [{"field": "dt", "value": "2025-01-03"}],
            sample,
        )
        token, request = calls[-1]
        ctx.require(token == "caller-token", "executor used another caller token")
        ctx.require(request["dataSource"] == "doris_ai", request)
        ctx.require(request["defaultLimit"] == 2, request)
        sql = " ".join(request["sql"].split())
        ctx.require("`id`" in sql and "`dt`" in sql, sql)
        ctx.require("amount" not in sql, f"unrequested column in sample SQL: {sql}")
        ctx.require("`dt` = '2025-01-03'" in sql, f"latest partition missing: {sql}")
        ctx.require("2025-01-01" not in sql, f"old partition used: {sql}")
        ctx.require(sql.endswith("LIMIT 2"), f"limit not applied: {sql}")

        default = ctx.mcp_call("sample_table_data", {"table": "shop.orders"})
        default_sql = " ".join(calls[-1][1]["sql"].split())
        for column in ("`id`", "`dt`", "`amount`", "`status`"):
            ctx.require(column in default_sql, f"default column {column} missing")
        ctx.require(default_sql.endswith("LIMIT 20"), default_sql)
        ctx.require(default["limit"] == 20, default)

        fact = ctx.mcp_call(
            "sample_table_data",
            {"table": "dm.sales_fact", "columns": ["id", "dt"], "limit": 2},
        )
        ctx.require(fact["row_count"] == 2, fact)
        ctx.require(fact["partition"] == [{"field": "dt", "value": "2026-08-31"}], fact)
        fact_sql = " ".join(fact["sql"].split())
        ctx.require("`dt` = '2026-08-31'" in fact_sql, fact_sql)
        ctx.require(fact_sql.endswith("LIMIT 2"), fact_sql)
        metadata_calls = [
            (token, request)
            for token, request in calls
            if "metadata_table" in request["sql"] and "sales_fact" in request["sql"]
        ]
        ctx.require(len(metadata_calls) == 1, f"unexpected metadata calls: {calls}")
        metadata_token, metadata_request = metadata_calls[0]
        ctx.require(metadata_token == "caller-token", "metadata used another token")
        ctx.require(metadata_request["dataSource"] == "doris_ai", metadata_request)
        ctx.require(1 <= metadata_request["defaultLimit"] <= 5, metadata_request)
        metadata_sql = " ".join(metadata_request["sql"].split())
        ctx.require(
            "mysql.data_tool_box_emr.metadata_table" in metadata_sql
            and "mysql.data_tool_box_emr.metadata_partition" in metadata_sql,
            metadata_sql,
        )
        ctx.require("database_name = 'dm'" in metadata_sql, metadata_sql)
        ctx.require("table_name = 'sales_fact'" in metadata_sql, metadata_sql)

        ctx.rest.put("/api/v1/me/dtb-token", json={"token": "expired"})
        expired = ctx.mcp_call(
            "sample_table_data", {"table": "shop.orders"}, allow_error=True
        )
        ctx.require(expired.isError, "expired DTB token was accepted")
        ctx.require(
            expired.structuredContent["code"] == "invalid_input",
            f"unexpected 401 code: {expired.structuredContent}",
        )
        ctx.require(
            "token" in expired.structuredContent["message"].lower(),
            "401 error does not mention the token",
        )

        missing_table = ctx.mcp_call(
            "sample_table_data", {"table": "missing.table"}, allow_error=True
        )
        ctx.require(missing_table.isError, "missing table was sampled")
        ctx.require(
            missing_table.structuredContent["code"] == "invalid_input",
            f"unexpected missing-table code: {missing_table.structuredContent}",
        )
        ctx.require(
            "知识代" in missing_table.structuredContent["message"],
            "missing-table error does not mention the knowledge generation",
        )

        audit = ctx.rest.get("/api/v1/query-audit")
        ctx.require(audit.status_code == 200, audit.text)
        rows = [row for row in audit.json() if row["kind"] == "sample"]
        succeeded = [row for row in rows if row["status"] == "succeeded"]
        ctx.require(len(succeeded) == 3, f"unexpected sample audit: {rows}")
        ctx.require(
            {row["sql"] for row in succeeded}
            == {sample["sql"], default["sql"], fact["sql"]},
            f"audit SQL differs: {rows}",
        )
        ctx.require(
            all(row["type"] == "取样" and row["row_count"] == 2 for row in succeeded),
            f"sample audit is not marked as sampling: {rows}",
        )
        ctx.require(
            any(row["status"] == "rejected" for row in rows),
            f"failed samples are not audited: {rows}",
        )
        return {
            "executor_calls": len(calls),
            "sample_audit": len(rows),
            "sql": sample["sql"],
        }
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
