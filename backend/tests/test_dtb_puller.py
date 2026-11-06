"""Contract checks for DTB metadata pagination and failures."""

import httpx
import pytest

from semantica_dataagent.plugins import discover


def puller():
    plugins = discover()
    assert ("puller", "dtb") in plugins, "DTB puller must be discoverable"
    return plugins[("puller", "dtb")]


def test_dtb_puller_fetches_all_pages():
    def reply(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "status": "200",
                "data": {
                    "tableList": [
                        {
                            "tableId": page,
                            "tableName": f"dim_{page}",
                            "databaseName": "dimensions",
                            "catalogType": "HIVE",
                        }
                    ],
                    "totalElements": 2,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        tables = puller().list_tables(client, "http://dtb", "secret")
    assert [row["tableId"] for row in tables] == [1, 2]


@pytest.mark.parametrize("failure", ["unauthorized", "timeout", "incomplete"])
def test_dtb_puller_rejects_failed_or_incomplete_response(failure):
    def reply(request):
        if failure == "unauthorized":
            return httpx.Response(401)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(
            200,
            json={
                "status": "200",
                "data": {
                    "tableList": [],
                    "totalElements": 2,
                },
            },
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
    obj.query_download = lambda client, base, token, sql, format, **kw: (
        tasks if format == "json" else output.getvalue()
    )
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
    obj.list_tables = lambda *args: [
        {
            "tableId": 1,
            "tableName": "dim_a",
            "databaseName": "dimensions",
            "catalogType": "HIVE",
        }
    ]
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


def _table(identity, database, name):
    return {
        "tableId": identity,
        "databaseName": database,
        "tableName": name,
        "catalogType": "HIVE",
    }


def _table_page_reply(pages, total, seen=None):
    def reply(request):
        assert request.url.path == "/api/data-map/table"
        assert request.url.params["limit"] == "500"
        page = int(request.url.params["page"])
        if seen is not None:
            seen.append(page)
        return httpx.Response(
            200,
            json={
                "status": "200",
                "data": {"tableList": pages.get(page, []), "total": total},
            },
        )

    return reply


def test_dimension_tables_are_found_after_non_dimension_pages():
    pages = {
        1: [_table(identity, "ods", f"ods_{identity}") for identity in range(1, 501)],
        2: [
            _table(501, "dimensions", "dimensions.dim_b"),
            _table(502, "dimensions", "dim_a"),
        ],
    }
    seen = []
    reply = _table_page_reply(pages, 502, seen)

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        tables = puller().list_tables(client, "http://dtb", "secret")

    assert [table["tableId"] for table in tables] == [501, 502]
    assert seen == [1, 2]


def test_bounded_pull_selects_sorted_dimensions_tables_only():
    import io
    import json

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["database_name", "table_name", "column_name", "column_type"])
    book.active.append(["dimensions", "dim_a", "id", "string"])
    xlsx = io.BytesIO()
    book.save(xlsx)
    tasks = json.dumps(
        {"columns": [{"name": "task_name"}, {"name": "task_src"}], "data": []}
    ).encode()
    pages = {
        1: [_table(identity, "ods", f"ods_{identity}") for identity in range(1, 501)],
        2: [
            _table(501, "dm", "dm_orders"),
            _table(502, "dimensions", "dimensions.z_orders"),
            _table(503, "dimensions", "dim_a"),
        ],
    }
    seen = []
    reply = _table_page_reply(pages, 503, seen)
    obj = puller()
    obj.table_detail = lambda *args: (
        {},
        [{"columnName": "id", "columnType": "string"}],
    )
    sqls = []

    def query(client, base, token, sql, format, **kwargs):
        sqls.append(sql)
        if format == "json":
            return tasks
        if format == "xlsx":
            return xlsx.getvalue()
        if "metadata_partition" in sql:
            return b"table_name,database_name,partition_name,records,rk\ndim_a,dimensions,,0,1\n"
        return b"id\n1\n"

    obj.query_download = query
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        obj.fetch(
            client, "http://dtb", "secret", {"layer": "dimensions", "max_tables": 1}
        )

    assert seen == [1, 2]
    assert len(sqls) == 4
    assert all("dim_a" in sql for sql in sqls)
    assert all(
        "ods_" not in sql and "dm_orders" not in sql and "z_orders" not in sql
        for sql in sqls
    )


def _bounded_pull_with_table_page(total_field, table_list, maximum=2):
    obj = puller()
    calls = []

    def reply(request):
        assert request.url.path == "/api/data-map/table"
        assert request.url.params["page"] == "1"
        assert request.url.params["limit"] == "500"
        data = {"tableList": table_list}
        if total_field is not None:
            data["total"] = total_field
        return httpx.Response(200, json={"status": "200", "data": data})

    def query(client, base, token, sql, fmt, **kwargs):
        calls.append(sql)
        raise RuntimeError("stop after observing scoped task query")

    obj.query_download = query
    with (
        httpx.Client(transport=httpx.MockTransport(reply)) as client,
        pytest.raises(RuntimeError),
    ):
        obj.fetch(
            client,
            "http://dtb",
            "secret",
            {"layer": "dimensions", "max_tables": maximum},
        )
    return calls


def test_bounded_pull_accepts_real_total_field_from_table_endpoint():
    calls = _bounded_pull_with_table_page(
        2,
        [
            _table(1, "dimensions", "dim_b"),
            _table(2, "dimensions", "dim_a"),
        ],
        maximum=2,
    )
    assert "dim_a" in calls[0] and "dim_b" in calls[0]


def test_pagination_falls_back_to_total_when_total_elements_is_null():
    def reply(request):
        return httpx.Response(
            200,
            json={
                "status": "200",
                "data": {
                    "tableList": [
                        {
                            "tableId": 1,
                            "tableName": "dim_a",
                            "databaseName": "dimensions",
                        }
                    ],
                    "totalElements": None,
                    "total": 1,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        tables = puller().list_tables(client, "http://dtb", "secret")
    assert [row["tableId"] for row in tables] == [1]


@pytest.mark.parametrize(
    "table_list",
    [
        [_table(1, "dimensions", "dim_a"), _table(2, "dimensions", "dim_b")],
        [_table(1, "dimensions", "dim_a")],
    ],
)
def test_bounded_pull_without_total_uses_page_size_as_end(table_list):
    calls = _bounded_pull_with_table_page(None, table_list)
    assert "dim_a" in calls[0]


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
