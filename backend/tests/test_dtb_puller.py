"""Contract checks for DTB metadata pagination and failures."""

import json

import httpx
import pytest

from semantica_dataagent.plugins import discover


def puller():
    plugins = discover()
    assert ("puller", "dtb") in plugins, "DTB puller must be discoverable"
    return plugins[("puller", "dtb")]


def _metadata_payload(rows):
    return json.dumps(
        {
            "columns": [
                {"name": "table_id", "type": "BIGINT"},
                {"name": "table_name", "type": "VARCHAR"},
            ],
            "data": rows,
        }
    ).encode()


def _query_reply(content, seen=None):
    def reply(request):
        assert request.url.path != "/api/data-map/table", "table pagination is unused"
        if request.url.path.endswith("/async-execute"):
            body = json.loads(request.content)
            if seen is not None:
                seen.append(body["sql"])
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "query-1"}}
            )
        if "/result/" in request.url.path:
            return httpx.Response(
                200, json={"status": "200", "data": {"progress": 1.0}}
            )
        assert request.url.path == "/api/data-query/download/query-1"
        return httpx.Response(200, content=content)

    return reply


def test_dtb_puller_reads_dimension_tables_from_metadata_sql():
    sqls = []
    payload = _metadata_payload(
        [
            ["1735", "dim_hotel_delivery_hotel_management_d"],
            ["1736", "dim_order"],
        ]
    )

    with httpx.Client(
        transport=httpx.MockTransport(_query_reply(payload, sqls))
    ) as client:
        tables = puller().list_tables(client, "http://dtb", "secret")

    assert tables == [
        {
            "tableId": "1735",
            "tableName": "dim_hotel_delivery_hotel_management_d",
        },
        {"tableId": "1736", "tableName": "dim_order"},
    ]
    assert len(sqls) == 1
    assert "mysql.data_tool_box_emr.metadata_table" in sqls[0]
    assert "database_name='dimensions'" in sqls[0]
    assert "catalog_type='HIVE'" in sqls[0]
    assert "status='在用'" in sqls[0]
    assert "ORDER BY table_name" in sqls[0]
    assert "LIMIT" not in sqls[0]


def test_dtb_puller_applies_maximum_to_metadata_sql():
    sqls = []
    payload = _metadata_payload([["501", "dim_a"]])

    with httpx.Client(
        transport=httpx.MockTransport(_query_reply(payload, sqls))
    ) as client:
        tables = puller().list_tables(client, "http://dtb", "secret", maximum=1)

    assert tables == [{"tableId": "501", "tableName": "dim_a"}]
    assert "ORDER BY table_name LIMIT 1" in sqls[0]


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"columns": [{"name": "table_id"}], "data": [["1"]]}).encode(),
        json.dumps(
            {
                "columns": [
                    {"name": "table_id"},
                    {"name": "table_name"},
                ],
                "data": [["1"]],
            }
        ).encode(),
        json.dumps(
            {
                "columns": [
                    {"name": "table_id"},
                    {"name": "table_name"},
                ],
                "data": [["1", "dim bad"]],
            }
        ).encode(),
        b"not-json",
    ],
)
def test_dtb_puller_rejects_malformed_metadata_sql_result(payload):
    with (
        httpx.Client(transport=httpx.MockTransport(_query_reply(payload))) as client,
        pytest.raises(ValueError),
    ):
        puller().list_tables(client, "http://dtb", "secret")


@pytest.mark.parametrize("failure", ["unauthorized", "timeout", "incomplete"])
def test_dtb_puller_rejects_failed_or_incomplete_response(failure):
    def reply(request):
        if failure == "unauthorized":
            return httpx.Response(401)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if request.url.path.endswith("/async-execute"):
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "query-1"}}
            )
        if "/result/" in request.url.path:
            return httpx.Response(
                200, json={"status": "200", "data": {"progress": 1.0}}
            )
        assert request.url.path == "/api/data-query/download/query-1"
        return httpx.Response(
            200,
            content=json.dumps(
                {"columns": [{"name": "table_id"}], "data": [["1", "dim_a"]]}
            ).encode(),
        )

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError),
    ):
        puller().list_tables(client, "http://dtb", "secret")


def test_dtb_puller_reads_table_detail_and_all_columns():
    calls = []

    def reply(request):
        calls.append(request.url.path)
        assert request.headers["token"] == "secret"
        assert request.headers["cookie"] == "satoken=secret"
        if request.url.path == "/api/data-map/detail/12":
            return httpx.Response(
                200,
                json={
                    "status": "200",
                    "data": {
                        "tableId": 12,
                        "tableName": "dimensions.dim_product",
                        "databaseName": "dimensions",
                    },
                },
            )
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "status": "200",
                "data": {
                    "columnList": [
                        {"columnName": f"col_{page}", "columnType": "STRING"}
                    ],
                    "totalElements": 2,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        detail, columns = puller().table_detail(client, "http://dtb", "secret", 12)
    assert detail["tableName"] == "dimensions.dim_product"
    assert [item["columnName"] for item in columns] == ["col_1", "col_2"]
    assert calls == [
        "/api/data-map/detail/12",
        "/api/data-map/detail/12/column",
        "/api/data-map/detail/12/column",
    ]


def test_dtb_puller_rejects_incomplete_columns():
    def reply(request):
        if request.url.path.endswith("/column"):
            return httpx.Response(
                200,
                json={
                    "status": "200",
                    "data": {
                        "columnList": [],
                        "totalElements": 1,
                    },
                },
            )
        return httpx.Response(200, json={"status": "200", "data": {"tableId": 12}})

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError, match="pagination"),
    ):
        puller().table_detail(client, "http://dtb", "secret", 12)


def test_query_download_waits_for_completion_and_returns_full_result():
    calls = []

    def reply(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("async-execute"):
            assert request.method == "POST"
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": "query-1"}}
            )
        if "/result/" in request.url.path:
            progress = 0.5 if calls.count(("GET", request.url.path)) == 1 else 1.0
            return httpx.Response(
                200, json={"status": "200", "data": {"progress": progress}}
            )
        assert request.url.params["format"] == "json"
        return httpx.Response(200, content=b'{"columns":[],"data":[]}')

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        content = puller().query_download(
            client, "http://dtb", "secret", "select 1", "json", poll_interval=0
        )
    assert content == b'{"columns":[],"data":[]}'
    assert calls == [
        ("POST", "/api/data-query/async-execute"),
        ("GET", "/api/data-query/result/query-1"),
        ("GET", "/api/data-query/result/query-1"),
        ("GET", "/api/data-query/download/query-1"),
    ]


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
            puller().list_tables(client, "http://dtb", "private-token")
        assert "private-token" not in str(error.value)
        assert "secret" not in str(error.value)


def test_fetch_validates_snapshot_before_returning():
    import io
    import json

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["database_name", "table_name", "column_name", "column_type"])
    book.active.append(["demo", "new_table", "id", "string"])
    output = io.BytesIO()
    book.save(output)
    tasks = json.dumps(
        {"columns": [{"name": "task_name"}, {"name": "task_src"}], "data": []}
    ).encode()

    def reply(request):
        if request.url.path == "/api/data-map/table":
            return httpx.Response(
                200,
                json={"status": "200", "data": {"tableList": [], "totalElements": 0}},
            )
        raise AssertionError(request.url.path)

    obj = puller()

    def query(client, base, token, sql, format, **kwargs):
        if "ORDER BY table_name" in sql:
            return _metadata_payload([])
        return tasks if format == "json" else output.getvalue()

    obj.query_download = query
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        files, counts = obj.fetch(client, "http://dtb", "secret")
    assert files["任务信息.json"] == tasks
    assert files["表信息.xlsx"] == output.getvalue()
    assert counts["tables"] == 1


def test_partitioned_dimension_reads_only_latest_and_checks_count():
    import io
    import json

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["database_name", "table_name", "column_name"])
    book.active.append(["dimensions", "dim_a", "id"])
    xlsx = io.BytesIO()
    book.save(xlsx)
    tasks = json.dumps(
        {"columns": [{"name": "task_name"}, {"name": "task_src"}], "data": []}
    ).encode()
    obj = puller()
    obj.list_tables = lambda *args: [{"tableId": 1, "tableName": "dim_a"}]
    obj.table_detail = lambda *args: (
        {},
        [
            {"columnName": "id", "columnType": "string"},
            {"columnName": "day_num", "columnType": "string", "seqNo": 10000},
        ],
    )
    sqls = []

    def query(client, base, token, sql, format, **kwargs):
        sqls.append(sql)
        if format == "xlsx":
            return xlsx.getvalue()
        if format == "json":
            return tasks
        if "metadata_partition" in sql:
            return b"table_name,database_name,partition_name,records,rk\ndim_a,dimensions,day_num=20261105,2,1\n"
        return b"id,day_num\n1,20261105\n2,20261105\n"

    obj.query_download = query
    with httpx.Client() as client:
        files, _ = obj.fetch(client, "http://dtb", "secret")
    assert any("WHERE day_num = '20261105'" in sql for sql in sqls)
    assert len(files["dimensions/dim_a/current/rows.jsonl"].splitlines()) == 2

    def incomplete(*args, **kwargs):
        result = query(*args, **kwargs)
        return (
            b"id,day_num\n1,20261105\n" if result.startswith(b"id,day_num") else result
        )

    obj.query_download = incomplete
    with httpx.Client() as client, pytest.raises(ValueError, match="count"):
        obj.fetch(client, "http://dtb", "secret")


def test_dtb_request_allowlist_blocks_network_before_sending():
    from semantica_dataagent.dtb_puller import checked_request

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        for method, path, body in [
            ("DELETE", "/api/data-map/table", None),
            ("GET", "/api/data-map/table/extra", None),
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
                {"sql": "SELECT 1", "dataSource": "doris_ai", "defaultLimit": 101},
            ),
        ]:
            with pytest.raises(ValueError):
                checked_request(client, method, "http://dtb", "token", path, json=body)
    assert calls == []


def test_bounded_pull_selects_sorted_dimensions_tables_only():
    import io

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["database_name", "table_name", "column_name", "column_type"])
    book.active.append(["dimensions", "dim_a", "id", "string"])
    xlsx = io.BytesIO()
    book.save(xlsx)
    tasks = json.dumps(
        {"columns": [{"name": "task_name"}, {"name": "task_src"}], "data": []}
    ).encode()
    obj = puller()
    obj.table_detail = lambda *args: (
        {},
        [{"columnName": "id", "columnType": "string"}],
    )
    sqls = []

    def query(client, base, token, sql, format, **kwargs):
        sqls.append(sql)
        if "ORDER BY table_name" in sql:
            assert "LIMIT 1" in sql
            return _metadata_payload([["503", "dim_a"]])
        if format == "json":
            return tasks
        if format == "xlsx":
            return xlsx.getvalue()
        if "metadata_partition" in sql:
            return b"table_name,database_name,partition_name,records,rk\ndim_a,dimensions,,0,1\n"
        return b"id\n1\n"

    obj.query_download = query
    with httpx.Client() as client:
        obj.fetch(
            client, "http://dtb", "secret", {"layer": "dimensions", "max_tables": 1}
        )

    assert len(sqls) == 5
    assert all("dim_a" in sql for sql in sqls[1:])
    assert all(
        "ods_" not in sql and "dm_orders" not in sql and "z_orders" not in sql
        for sql in sqls
    )


def test_unscoped_fetch_uses_metadata_sql_without_limit():
    import io

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["database_name", "table_name", "column_name", "column_type"])
    book.active.append(["dimensions", "dim_a", "id", "string"])
    xlsx = io.BytesIO()
    book.save(xlsx)
    tasks = json.dumps(
        {"columns": [{"name": "task_name"}, {"name": "task_src"}], "data": []}
    ).encode()
    obj = puller()
    obj.table_detail = lambda *args: (
        {},
        [{"columnName": "id", "columnType": "string"}],
    )
    sqls = []

    def query(client, base, token, sql, format, **kwargs):
        sqls.append(sql)
        if "ORDER BY table_name" in sql:
            assert "LIMIT" not in sql
            return _metadata_payload([["504", "dim_a"]])
        if format == "json":
            return tasks
        if format == "xlsx":
            return xlsx.getvalue()
        if "metadata_partition" in sql:
            return b"table_name,database_name,partition_name,records,rk\ndim_a,dimensions,,0,1\n"
        return b"id\n1\n"

    obj.query_download = query
    with httpx.Client() as client:
        obj.fetch(client, "http://dtb", "secret")

    assert len(sqls) == 5
    assert all("dim_a" in sql for sql in sqls[1:])


@pytest.mark.parametrize("status_code", [401, 415, 503])
def test_dtb_puller_recognizes_http_authentication_failure(status_code):
    def reply(request):
        return httpx.Response(status_code)

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError, match="authentication") as error,
    ):
        puller().list_tables(client, "http://dtb", "secret")
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
        puller().list_tables(client, "http://dtb", "secret")
    assert body_status in str(error.value)


def test_query_download_recognizes_body_authentication_failure():
    def reply(request):
        return httpx.Response(200, json={"status": "503", "data": None})

    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(ValueError, match="authentication"),
    ):
        puller().query_download(
            client, "http://dtb", "secret", "select 1", "json", poll_interval=0
        )
