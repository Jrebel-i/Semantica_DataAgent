"""Verify a deployed cluster using an admin token supplied in an environment variable."""

import argparse
import asyncio
import json
import os
import subprocess
import threading

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = "http://172.16.242.124:31630"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "baize@172.16.242.124"]


def session_pods():
    result = subprocess.run(
        [
            *SSH,
            "kubectl -n semantica-dataagent get pods -l app.kubernetes.io/component=agent-session -o json",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return {p["metadata"]["name"] for p in json.loads(result.stdout)["items"]}


async def search(token, base):
    async with (
        httpx.AsyncClient(
            headers={"Authorization": "Bearer " + token}, timeout=60, trust_env=False
        ) as client,
        streamable_http_client(base + "/mcp/", http_client=client) as (
            read,
            write,
            _,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "search_knowledge", {"query": "销售额", "scope": "docs", "top_k": 5}
        )
        assert not result.isError, "MCP search failed"
        value = result.structuredContent
        if isinstance(value, dict):
            value = value.get("result", value)
        if not value and result.content:
            value = json.loads(result.content[0].text)
        assert isinstance(value, list) and value, "MCP search returned no results"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    token = os.environ["DATAAGENT_TOKEN"]
    stop = threading.Event()
    observed = set()
    baseline = session_pods()

    def watch():
        while not stop.wait(0.5):
            observed.update(session_pods() - baseline)

    watcher = threading.Thread(target=watch, daemon=True)
    with httpx.Client(
        base_url=base,
        headers={"Authorization": "Bearer " + token},
        timeout=600,
        trust_env=False,
    ) as client:
        assert client.get("/").status_code == 200
        providers = client.get("/api/v1/providers").json()
        provider = next(p for p in providers if p["name"] == "公司模型网关")
        watcher.start()
        try:
            with client.stream(
                "POST",
                "/api/v1/conversations/ask",
                json={
                    "agent": "pi",
                    "provider_id": provider["id"],
                    "model": provider["models"][0],
                    "question": "知识库里支付销售额的口径是什么？先用 search_knowledge 查询，再用 shop.payments 写按 dt 统计记录数的 Hive SQL，调用 validate_sql 验证并附引用。",
                },
            ) as response:
                response.raise_for_status()
                data = response.read().decode()
            events = []
            for block in data.split("\n\n"):
                lines = block.splitlines()
                name = next(
                    (line[7:] for line in lines if line.startswith("event: ")), None
                )
                payload = "\n".join(
                    line[6:] for line in lines if line.startswith("data: ")
                )
                if name and payload:
                    events.append((name, json.loads(payload)))
            assert token not in data, "token leaked in response"
            done = next(
                (value for name, value in reversed(events) if name == "done"), None
            )
            assert done and done.get("sql") and done.get("citations"), (
                "answer lacks SQL or citations"
            )
            print("online Q&A passed; conversation", done["conversation_id"])
        finally:
            stop.set()
            watcher.join(timeout=20)
        assert observed, "no session Pod observed"
        remaining = session_pods() & observed
        assert not remaining, f"session Pods remain: {remaining}"
        print("session Pod cleanup passed")
    asyncio.run(search(token, base))
    print("MCP search passed")


if __name__ == "__main__":
    main()
