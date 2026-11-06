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
                            "catalogType": "HIVE",
                        }
                    ],
                    "totalElements": 2,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        tables = puller().list_tables(client, "http://dtb", "secret", "dimensions")
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
        puller().list_tables(client, "http://dtb", "secret", "dimensions")


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
