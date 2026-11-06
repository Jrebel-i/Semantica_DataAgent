import asyncio

import httpx
import pytest

from semantica_dataagent.dtb_executor import (
    DtbAuthenticationError,
    DtbError,
    DtbExecutor,
    column_names,
)


def test_dtb_query_protocol_and_caller_token():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("async-execute"):
            return httpx.Response(200, json={"status": 200, "data": {"queryKey": "q1"}})
        if "/result/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "data": {
                        "progress": 1.0,
                        "columns": [{"name": "n", "type": "long"}],
                        "data": [[1]],
                    },
                },
            )
        return httpx.Response(200, content=b"n\n1\n")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            executor = DtbExecutor("http://dtb.test", "my-token", client=client)
            key = await executor.submit("select 1", 1000)
            result = await executor.poll(key)
            content, _ = await executor.download(key, "csv")
            assert result["data"] == [[1]]
            assert content == b"n\n1\n"

    asyncio.run(run())
    import json

    assert json.loads(calls[0].content) == {
        "env": 2,
        "dataSource": "doris_ai",
        "sql": "select 1",
        "defaultLimit": 1000,
    }
    assert all(call.headers["token"] == "my-token" for call in calls)


def test_column_names_accepts_strings_and_named_objects():
    assert column_names(["a", "b"]) == ["a", "b"]
    assert column_names([{"name": "a", "type": "long"}, {"name": "b"}]) == ["a", "b"]
    assert column_names([{"type": "long"}]) is None
    assert column_names(["a", {"type": "long"}]) is None
    assert column_names("a") is None


def test_dtb_recognizes_auth_and_query_errors():
    async def run():
        for response, expected in [
            (httpx.Response(401), DtbAuthenticationError),
            (httpx.Response(415), DtbAuthenticationError),
            (httpx.Response(503), DtbAuthenticationError),
            (
                httpx.Response(
                    200,
                    json={
                        "status": "503",
                        "message": "未登录或token过期",
                        "data": None,
                    },
                ),
                DtbAuthenticationError,
            ),
            (
                httpx.Response(
                    200,
                    json={
                        "status": 200,
                        "data": {"progress": 1, "errorMessage": "bad sql"},
                    },
                ),
                DtbError,
            ),
        ]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _, response=response: response)
            ) as client:
                executor = DtbExecutor(
                    "http://dtb.test", "token", client=client, retry_delays=()
                )
                with pytest.raises(expected):
                    await executor.poll("q")

    asyncio.run(run())


def test_disallowed_dtb_requests_never_reach_transport():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": 200, "data": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            executor = DtbExecutor(
                "http://dtb.test", "token", client=client, retry_delays=()
            )
            for method, path in [
                ("DELETE", "/api/data-query/result/q"),
                ("GET", "/api/data-query/async-execute"),
                ("POST", "/api/data-map/table"),
                ("GET", "/api/data-query/result/q/extra"),
                ("GET", "/api/data-query/download-evil"),
                ("GET", "/api/data-query/result/../admin"),
                ("GET", "/api/data-query/result/%2e%2e"),
                ("GET", "//evil.test/api/data-query/result/q"),
                ("GET", "/api/data-query/result/q?x=y"),
            ]:
                with pytest.raises(ValueError):
                    await executor._request(method, path)
            assert calls == []

    asyncio.run(run())
