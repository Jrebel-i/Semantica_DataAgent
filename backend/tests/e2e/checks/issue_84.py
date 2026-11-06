"""通过 REST 验证会话运行时可以回答并继续对话。"""

import importlib.util
import os
import subprocess
import threading
from pathlib import Path


def check_session_runtime(ctx):
    spec = importlib.util.spec_from_file_location(
        "issue_19_checks", Path(__file__).with_name("issue_19.py")
    )
    checks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checks)
    response = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": "Issue 84 pi",
            "type": "anthropic-messages",
            "base_url": os.getenv("TEST_PI_BASE_URL", "http://127.0.0.1:3425"),
            "models": [os.getenv("TEST_PI_MODEL", "deepseek-v4.1-flash#claude")],
        },
    )
    ctx.require(response.status_code == 201, response.text)
    observed = threading.Event()
    stop = threading.Event()
    failures = []

    def inspect():
        while not stop.wait(0.2):
            ids = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    "label=com.semantica-dataagent.agent=1",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            for name in ids:
                if name.endswith("-relay"):
                    continue
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        name,
                        "node",
                        "-e",
                        "require('dns').resolve4('example.com', (e) => process.exit(e ? 0 : 1)); setTimeout(() => process.exit(0), 3000)",
                    ],
                    capture_output=True,
                    check=False,
                    timeout=8,
                )
                if result.returncode:
                    failures.append("会话容器可以执行 DNS 查询")
                observed.set()
                return

    watcher = None
    if os.getenv("DATAAGENT_AGENT_MODE", "sandbox") != "local":
        watcher = threading.Thread(target=inspect, daemon=True)
        watcher.start()
    try:
        first = checks._ask(
            ctx,
            {
                "agent": "pi",
                "provider_id": response.json()["id"],
                "model": os.getenv("TEST_PI_MODEL", "deepseek-v4.1-flash#claude"),
                "question": "请用 search_knowledge 查询支付销售额的口径并引用依据。",
            },
        )
    finally:
        stop.set()
        if watcher:
            watcher.join()
    ctx.require(not failures, str(failures))
    if watcher:
        ctx.require(observed.is_set(), "没有观察到 Docker 会话容器")
    ctx.require(
        first and first[0][0] == "conversation" and first[-1][0] == "done",
        f"首轮失败: {first[-1:]}",
    )
    second = checks._ask(
        ctx,
        {
            "conversation_id": first[0][1]["conversation_id"],
            "question": "请简要复述上一轮的答案。",
        },
    )
    ctx.require(second and second[-1][0] == "done", f"追问失败: {second[-1:]}")
    ctx.require(any(name == "tool_start" for name, _ in first), "首轮没有使用 MCP 工具")
    return {"turns": 2, "dns_blocked": bool(watcher)}
