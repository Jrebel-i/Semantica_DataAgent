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

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError):
            puller().list_tables(client, "http://dtb", "secret", "dimensions")
