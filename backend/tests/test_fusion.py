from semantica_dataagent import knowledge


def test_fusion_combines_sources_and_demotes_outputless_tasks():
    hits = {
        "exact": [{"ref": "task:a", "name": "a", "has_output": True}],
        "fulltext": [{"ref": "task:a", "name": "a", "has_output": True}],
        "vector": [{"ref": "task:b", "name": "b", "has_output": False}],
        "graph": [],
    }
    result = knowledge.fuse(hits, 10)
    assert [x["ref"] for x in result] == ["task:a", "task:b"]
    assert result[0]["sources"] == ["exact", "fulltext"]
    assert result[0]["confidence"] == "high"
    assert result[1]["confidence"] == "low"


def test_mcp_contract_rejects_extra_and_hides_internal_errors():
    import asyncio

    from mcp.types import CallToolRequest, CallToolRequestParams

    from semantica_dataagent.app import create_mcp

    async def run():
        server = create_mcp()
        handler = server._mcp_server.request_handlers[CallToolRequest]
        response = await handler(
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name="describe_tables", arguments={"names": [], "extra": 1}
                ),
            )
        )
        assert response.root.isError
        assert response.root.structuredContent["code"] == "invalid_input"

    asyncio.run(run())


def test_matching_metric_in_multiple_documents_marks_possible_ambiguity():
    sources = {
        "fulltext": [
            {
                "ref": "document:metrics/a#chunk-1",
                "doc_id": "document:metrics/a",
                "content": "销售额按支付金额统计",
            },
            {
                "ref": "document:metrics/b#chunk-1",
                "doc_id": "document:metrics/b",
                "content": "销售额按下单金额统计",
            },
        ]
    }
    result = knowledge.mark_ambiguity(knowledge.fuse(sources, 10), "销售额")
    assert all(item["ambiguous"] for item in result)
    assert set(result[0]["ambiguity_refs"]) == {
        "document:metrics/a#chunk-1",
        "document:metrics/b#chunk-1",
    }


def test_mcp_pagination_and_sanitized_error():
    import asyncio
    import json

    from mcp.types import CallToolRequest, CallToolRequestParams

    from semantica_dataagent.app import create_mcp

    async def run():
        server = create_mcp()

        async def fake_call(name, arguments):
            if arguments.get("names") == ["bad"]:
                raise RuntimeError("private backend detail")
            return {"result": [{"name": str(i)} for i in range(30)]}

        server.call_tool = fake_call
        handler = server._mcp_server.request_handlers[CallToolRequest]

        async def call(arguments):
            response = await handler(
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="describe_tables", arguments=arguments
                    ),
                )
            )
            return response.root

        page = await call({"names": []})
        assert len(page.structuredContent["result"]) == 25
        continuation = await call(
            {"names": [], "cursor": page.structuredContent["next_cursor"]}
        )
        assert len(continuation.structuredContent["result"]) == 5
        bad = await call({"names": ["bad"]})
        assert bad.isError and bad.structuredContent["code"] == "internal_error"
        assert "private backend detail" not in json.dumps(bad.model_dump())

    asyncio.run(run())
