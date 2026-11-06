"""Contract checks for the DTB dimensions puller."""

import io
import json
import re

import httpx
import pytest
from openpyxl import Workbook

from semantica_dataagent.plugins import discover

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


def partition_row(table_id, table_name, partition_name="", records="1", rk="1"):
    return ",".join(
        [
            str(table_id),
            table_name,
            "dimensions",
            "",
            "",
            "",
            "",
            partition_name,
            records,
            "",
            rk,
        ]
    )


def puller():
    plugins = discover()
    assert ("puller", "dtb") in plugins, "DTB puller must be discoverable"
    return plugins[("puller", "dtb")]


def metadata_row(
    table_id,
    table_name,
    column_name=None,
    seq_no=None,
    column_type=None,
    comment=None,
    business=None,
    database="dimensions",
):
    return [
        str(table_id),
        table_name,
        database,
        None,
        None,
        None,
        None,
        None,
        None,
        column_name,
        seq_no,
        column_type,
        comment,
        business,
    ]


def metadata_payload(rows):
    return json.dumps(
        {
            "columns": [{"name": name, "type": "VARCHAR"} for name in METADATA_COLUMNS],
            "data": rows,
        }
    ).encode()


def _table_workbook(rows):
    book = Workbook()
    book.active.append(["database_name", "table_name", "column_name", "column_type"])
    for row in rows:
        book.active.append(row)
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


class FakeDtb:
    """Serve the SQL protocol used by the puller, truncating like real DTB."""

    def __init__(
        self,
        *,
        metadata_rows,
        stats="",
        table_rows=None,
        dimension_rows=None,
        auth_keys=(),
        task=None,
    ):
        self.metadata_rows = metadata_rows
        self.stats = stats
        self.table_rows = table_rows or self._default_table_rows(metadata_rows)
        self.dimension_rows = dimension_rows or {}
        self.auth_keys = set(auth_keys)
        self.task = (
            task
            or json.dumps(
                {
                    "columns": [{"name": "task_name"}, {"name": "task_src"}],
                    "data": [],
                }
            ).encode()
        )
        self.paths = []
        self.sqls = {}
        self.limits = {}
        self.formats = []

    @staticmethod
    def _default_table_rows(metadata_rows):
        seen = set()
        rows = []
        for row in metadata_rows:
            table_id, table_name = row[0], row[1]
            column_name, column_type = row[9], row[11]
            if table_id in seen or column_name is None:
                continue
            seen.add(table_id)
            rows.append(["dimensions", table_name, column_name, column_type])
        return rows

    @staticmethod
    def _key(sql):
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

    def __call__(self, request):
        self.paths.append((request.method, request.url.path))
        if request.url.path == "/api/data-query/async-execute":
            body = json.loads(request.content)
            assert body["dataSource"] == "doris_ai"
            key = self._key(body["sql"])
            if key in self.auth_keys:
                return httpx.Response(
                    200,
                    json={
                        "status": "503",
                        "message": "未登录或token过期",
                        "data": None,
                    },
                )
            self.sqls.setdefault(key, []).append(body["sql"])
            self.limits.setdefault(key, []).append(body["defaultLimit"])
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": key}}
            )
        if request.url.path.startswith("/api/data-query/result/"):
            return httpx.Response(
                200, json={"status": "200", "data": {"progress": 1.0}}
            )
        if request.url.path.startswith("/api/data-query/download/"):
            key = request.url.path.rsplit("/", 1)[-1]
            format = request.url.params.get("format")
            self.formats.append((key, format))
            limit = self.limits[key][-1]
            content, content_type = self._download(key, format, limit)
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": content_type},
            )
        raise AssertionError(f"Unexpected DTB request: {request.method} {request.url}")

    def _download(self, key, format, limit):
        if key == "metadata":
            rows = self.metadata_rows[:limit] if limit else self.metadata_rows
            return metadata_payload(rows), "application/json"
        if key == "task":
            if not limit:
                return self.task, "application/json"
            payload = json.loads(self.task)
            payload["data"] = payload["data"][:limit]
            return json.dumps(payload).encode(), "application/json"
        if key == "stats":
            lines = self.stats.splitlines(keepends=True)
            if limit:
                lines = lines[: limit + 1]
            return "".join(lines).encode(), "text/csv"
        if key.startswith("dim_"):
            name = key.removeprefix("dim_")
            rows = self.dimension_rows.get(name, [["id"], ["1"]])
            header, data = rows[0], rows[1:]
            if limit:
                data = data[:limit]
            if format == "json":
                return json.dumps({"columns": [{"name": col, "type": "VARCHAR"} for col in header], "data": data}).encode(), "application/json"
            content = "\n".join(",".join("" if v is None else v for v in row) for row in [header, *data]) + "\n"
            return content.encode(), "text/csv"
        sql = self.sqls["table"][-1]
        rows = self.table_rows
        match = re.search(r"a\.table_name IN \(([^)]*)\)", sql)
        if match:
            allowed = set(re.findall(r"'([^']+)'", match.group(1)))
            rows = [row for row in rows if row[1] in allowed]
        if limit:
            rows = rows[:limit]
        return (
            _table_workbook(rows),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def run_fetch(fake, parameters=None):
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        return puller().fetch(client, "http://dtb", "secret", parameters)


def test_dimensions_metadata_uses_one_unlimited_json_query():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_a", "id", 1, "string", "编号", "业务编号"),
            metadata_row(1, "dim_a", "day_num", 10001, "string"),
            metadata_row(2, "dim_b", "id", 1, "string"),
        ]
    )

    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        tables = puller().fetch_dimensions_metadata(client, "http://dtb", "secret")

    assert [table["tableName"] for table in tables] == ["dim_a", "dim_b"]
    assert [column["name"] for column in tables[0]["columns"]] == [
        "id",
        "day_num",
    ]
    assert tables[0]["columns"][1]["seq_no"] == 10001
    assert tables[0]["columns"][0]["business_comment"] == "业务编号"
    assert fake.limits["metadata"] == [0]
    assert fake.formats == [("metadata", "json")]
    assert "metadata_column" in fake.sqls["metadata"][0]
    assert "ORDER BY a.table_id, b.seq_no" in fake.sqls["metadata"][0]
    assert not any(path.startswith("/api/data-map") for _, path in fake.paths)


def test_dimensions_metadata_keeps_every_row_when_default_limit_is_zero():
    rows = [
        metadata_row(1, "dim_a", f"column_{index}", index, "string")
        for index in range(1, 131)
    ]
    fake = FakeDtb(metadata_rows=rows)

    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        tables = puller().fetch_dimensions_metadata(client, "http://dtb", "secret")

    assert fake.limits["metadata"] == [0]
    assert len(tables[0]["columns"]) == 130


def test_dimensions_metadata_marks_invalid_column_metadata_per_table():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_good", "id", 1, "string"),
            metadata_row(2, "dim_bad", "id", 1, ""),
        ]
    )

    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        tables = puller().fetch_dimensions_metadata(client, "http://dtb", "secret")

    assert tables[0]["error"] is None
    assert tables[1]["error"] == "invalid_column_metadata"


def test_max_tables_selects_first_tables_by_name_and_scopes_queries():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(3, "dim_c", "id", 1, "string"),
            metadata_row(1, "dim_a", "id", 1, "string"),
            metadata_row(2, "dim_b", "id", 1, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(1, "dim_a")
            + "\n"
            + partition_row(2, "dim_b")
            + "\n"
            + partition_row(3, "dim_c")
            + "\n"
        ),
    )

    files, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 2})

    assert counts["failures"] == []
    assert counts["tables"] == 2
    assert sorted(name for name in files if name.startswith("dimensions/")) == [
        "dimensions/dim_a/current/rows.jsonl",
        "dimensions/dim_a/meta.json",
        "dimensions/dim_b/current/rows.jsonl",
        "dimensions/dim_b/meta.json",
    ]
    assert "output_table_name IN ('dim_a','dim_b')" in fake.sqls["task"][-1]
    assert "a.table_name IN ('dim_a','dim_b')" in fake.sqls["table"][-1]
    assert "table_name IN ('dim_a','dim_b')" in fake.sqls["stats"][-1]
    assert "LIMIT" not in fake.sqls["metadata"][-1]
    assert fake.limits["metadata"] == [0]
    assert fake.limits["task"] == [0]
    assert fake.limits["table"] == [0]
    assert fake.limits["stats"] == [0]
    assert fake.limits["dim_dim_a"] == [100]
    assert fake.limits["dim_dim_b"] == [100]
    assert not any(path.startswith("/api/data-map") for _, path in fake.paths)


def test_unpartitioned_table_reads_the_full_table_with_limit():
    fake = FakeDtb(
        metadata_rows=[metadata_row(1, "dim_a", "id", 1, "string")],
        stats=PARTITION_HEADER + "\n" + partition_row(1, "dim_a") + "\n",
        dimension_rows={"dim_a": [["id"], ["1"], ["2"]]},
    )

    files, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 1})

    assert counts["failures"] == []
    assert fake.sqls["dim_dim_a"][-1] == (
        "SELECT * FROM hive.dimensions.dim_a LIMIT 100"
    )
    meta = json.loads(files["dimensions/dim_a/meta.json"])
    assert meta["partition_field"] is None
    assert meta["record_count"] == 2


def test_partitioned_table_reads_only_the_latest_partition():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_a", "id", 1, "string"),
            metadata_row(1, "dim_a", "day_num", 10001, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(1, "dim_a", "day_num=20261105")
            + "\n"
        ),
        dimension_rows={"dim_a": [["id", "day_num"], ["1", "20261105"]]},
    )

    files, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 1})

    assert counts["failures"] == []
    assert "WHERE day_num = '20261105' LIMIT 100" in fake.sqls["dim_dim_a"][-1]
    meta = json.loads(files["dimensions/dim_a/meta.json"])
    assert meta["partition_field"] == "day_num"
    assert meta["latest_partition"] == "20261105"


def test_dimension_at_the_row_limit_is_marked_truncated():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_a", "id", 1, "string"),
            metadata_row(1, "dim_a", "day_num", 10001, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(1, "dim_a", "day_num=20261105", "200")
            + "\n"
        ),
        dimension_rows={
            "dim_a": [["id", "day_num"]]
            + [[str(index), "20261105"] for index in range(100)]
        },
    )

    files, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 1})

    assert counts["failures"] == []
    meta = json.loads(files["dimensions/dim_a/meta.json"])
    assert meta["record_count"] == 100
    assert meta["truncated"] is True


def test_multiple_partition_fields_fail_only_that_table():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_bad", "day_num", 10001, "string"),
            metadata_row(1, "dim_bad", "hour_num", 10002, "string"),
            metadata_row(2, "dim_good", "day_num", 10001, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(1, "dim_bad", "day_num=20261105")
            + "\n"
            + partition_row(2, "dim_good", "day_num=20261105")
            + "\n"
        ),
        dimension_rows={"dim_good": [["day_num"], ["20261105"]]},
    )

    files, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 2})

    assert counts["failures"] == [
        {"table": "dim_bad", "reason": "multiple_partition_fields"}
    ]
    assert "dimensions/dim_bad/meta.json" not in files
    assert "dimensions/dim_good/meta.json" in files


def test_partition_stats_field_mismatch_fails_only_that_table():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_bad", "day_num", 10001, "string"),
            metadata_row(2, "dim_good", "day_num", 10001, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(1, "dim_bad", "month_num=202611")
            + "\n"
            + partition_row(2, "dim_good", "day_num=20261105")
            + "\n"
        ),
        dimension_rows={"dim_good": [["day_num"], ["20261105"]]},
    )

    files, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 2})

    assert counts["failures"] == [
        {"table": "dim_bad", "reason": "partition_stats_field_mismatch"}
    ]
    assert "dimensions/dim_bad/meta.json" not in files
    assert "dimensions/dim_good/meta.json" in files


def test_missing_partition_stats_fails_only_that_table():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_bad", "day_num", 10001, "string"),
            metadata_row(2, "dim_good", "day_num", 10001, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(2, "dim_good", "day_num=20261105")
            + "\n"
        ),
        dimension_rows={"dim_good": [["day_num"], ["20261105"]]},
    )

    _, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 2})

    assert counts["failures"] == [
        {"table": "dim_bad", "reason": "partition_stats_missing"}
    ]


def test_all_tables_failed_raises_and_publishes_nothing():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_a", "day_num", 10001, "string"),
            metadata_row(1, "dim_a", "hour_num", 10002, "string"),
            metadata_row(2, "dim_b", "day_num", 10001, "string"),
            metadata_row(2, "dim_b", "hour_num", 10002, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(1, "dim_a", "day_num=20261105")
            + "\n"
            + partition_row(2, "dim_b", "day_num=20261105")
            + "\n"
        ),
    )

    with pytest.raises(ValueError, match="All DTB dimension tables failed"):
        run_fetch(fake)


def test_authentication_failure_stops_the_whole_pull():
    fake = FakeDtb(
        metadata_rows=[
            metadata_row(1, "dim_a", "id", 1, "string"),
            metadata_row(2, "dim_b", "id", 1, "string"),
        ],
        stats=(
            PARTITION_HEADER
            + "\n"
            + partition_row(1, "dim_a")
            + "\n"
            + partition_row(2, "dim_b")
            + "\n"
        ),
        auth_keys={"dim_dim_a"},
    )

    with pytest.raises(ValueError, match="authentication"):
        run_fetch(fake)


@pytest.mark.parametrize("auth_key", ["metadata", "task", "table", "stats"])
def test_authentication_failure_at_metadata_stage_stops_immediately(auth_key):
    fake = FakeDtb(
        metadata_rows=[metadata_row(1, "dim_a", "id", 1, "string")],
        auth_keys={auth_key},
    )

    with pytest.raises(ValueError, match="authentication"):
        run_fetch(fake)


def test_query_download_rejects_nonzero_result_at_the_row_limit():
    rows = [["1"], ["2"], ["3"]]

    def reply(request):
        if request.url.path.endswith("/async-execute"):
            body = json.loads(request.content)
            assert body["defaultLimit"] == 3
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "query-1"}}
            )
        if "/result/" in request.url.path:
            return httpx.Response(
                200, json={"status": "200", "data": {"progress": 1.0}}
            )
        return httpx.Response(
            200,
            content=json.dumps({"columns": [{"name": "id"}], "data": rows}).encode(),
        )

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError, match="DTB result truncated"),
    ):
        puller().query_download(
            client,
            "http://dtb",
            "secret",
            "select 1",
            "json",
            poll_interval=0,
            row_limit=3,
        )


def test_query_download_does_not_download_failed_query():
    def reply(request):
        if request.url.path.endswith("async-execute"):
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "query-1"}}
            )
        assert "/download/" not in request.url.path
        return httpx.Response(
            200, json={"status": "200", "data": {"errorMessage": "failed"}}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError, match="query"),
    ):
        puller().query_download(
            client, "http://dtb", "secret", "select 1", "json", poll_interval=0
        )


def test_dtb_request_allowlist_blocks_network_before_sending():
    from semantica_dataagent.dtb_puller import checked_request

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        for method, path, body in [
            ("GET", "/api/data-map/table", None),
            ("GET", "/api/data-map/detail/12", None),
            ("GET", "/api/data-map/detail/12/column", None),
            ("DELETE", "/api/data-map/table", None),
            (
                "POST",
                "/api/data-query/async-execute",
                {"sql": "DELETE FROM x", "dataSource": "doris_ai", "defaultLimit": 10},
            ),
            (
                "POST",
                "/api/data-query/async-execute",
                {"sql": "SELECT 1", "dataSource": "other", "defaultLimit": 10},
            ),
            (
                "POST",
                "/api/data-query/async-execute",
                {
                    "sql": "SELECT 1",
                    "dataSource": "doris_ai",
                    "defaultLimit": 500001,
                },
            ),
        ]:
            with pytest.raises(ValueError):
                checked_request(client, method, "http://dtb", "token", path, json=body)
        checked_request(
            client,
            "POST",
            "http://dtb",
            "token",
            "/api/data-query/async-execute",
            json={"sql": "SELECT 1", "dataSource": "doris_ai", "defaultLimit": 0},
        )
    assert len(calls) == 1


def test_fetch_validates_snapshot_before_returning():
    fake = FakeDtb(
        metadata_rows=[],
        table_rows=[["dimensions", "new_table", "id", "string"]],
    )

    files, counts = run_fetch(fake)

    assert files["任务信息.json"]
    assert files["表信息.xlsx"]
    assert counts["tables"] == 1
    assert counts["failures"] == []


@pytest.mark.parametrize("status_code", [401, 415, 503])
def test_dtb_puller_recognizes_http_authentication_failure(status_code):
    def reply(request):
        return httpx.Response(status_code)

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError, match="authentication") as error,
    ):
        puller().fetch_dimensions_metadata(client, "http://dtb", "secret")
    assert str(status_code) in str(error.value)


@pytest.mark.parametrize("body_status", ["401", "415", "503"])
def test_dtb_puller_recognizes_body_authentication_failure(body_status):
    def reply(request):
        return httpx.Response(
            200,
            json={
                "status": body_status,
                "message": "未登录或token过期",
                "data": None,
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError, match="authentication") as error,
    ):
        puller().fetch_dimensions_metadata(client, "http://dtb", "secret")
    assert body_status in str(error.value)


def test_authentication_and_timeout_have_distinct_safe_errors():
    def unauthorized(request):
        return httpx.Response(401, text="secret response")

    def timeout(request):
        raise httpx.ReadTimeout("secret request", request=request)

    for handler, expected in ((unauthorized, "401"), (timeout, "timeout")):
        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
            pytest.raises(ValueError, match=expected) as error,
        ):
            puller().fetch_dimensions_metadata(client, "http://dtb", "private-token")
        assert "private-token" not in str(error.value)
        assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps({"columns": [], "data": []}).encode(),
        json.dumps(
            {
                "columns": [{"name": name} for name in METADATA_COLUMNS],
                "data": [["not-a-list"]],
            }
        ).encode(),
    ],
)
def test_fetch_rejects_malformed_dimensions_metadata(payload):
    def reply(request):
        if request.url.path.endswith("/async-execute"):
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "query-1"}}
            )
        if "/result/" in request.url.path:
            return httpx.Response(
                200, json={"status": "200", "data": {"progress": 1.0}}
            )
        return httpx.Response(200, content=payload)

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError),
    ):
        puller().fetch_dimensions_metadata(client, "http://dtb", "secret")


def test_dimension_json_preserves_special_values():
    values = ['say "hello",\nworld', '', None]
    fake = FakeDtb(metadata_rows=[metadata_row(1, "dim_special", "value", 1, "string")],
                   stats=PARTITION_HEADER + "\n",
                   dimension_rows={"dim_special": [["value"], *[[v] for v in values]]})
    files, counts = run_fetch(fake, {"layer": "dimensions", "max_tables": 1})
    assert counts["failures"] == []
    rows = [json.loads(line) for line in files["dimensions/dim_special/rows.jsonl"].splitlines()]
    assert rows == [{"value": v} for v in values]
