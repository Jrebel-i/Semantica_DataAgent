"""Evaluate agent SQL through the application's query interfaces."""

import json
import os
import time
from collections import Counter

import httpx

from .eval import compare_results, load_questions


def _failure(status):
    if status in (401, 403):
        return "鉴权失败"
    if status in (408, 504):
        return "超时"
    if status in (400, 422):
        return "校验被拒"
    return "DTB 或服务错误"


def _execute(client, sql):
    started = time.monotonic()
    try:
        plan = client.post("/api/v1/queries/plan", json={"sql": sql})
        if not plan.is_success:
            return {"status": "failed", "reason": _failure(plan.status_code)}
        data = plan.json()
        response = client.post(f"/api/v1/queries/{data['id']}/execute", json={})
        if not response.is_success:
            return {"status": "failed", "reason": _failure(response.status_code)}
        result = response.json()
        rows = result["rows"]
        return {
            "status": "succeeded",
            "row_count": result["row_count"],
            "column_count": len(result["columns"]),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "result": {
                "columns": result["columns"],
                "rows": rows,
                "truncated": result["row_count"] > len(rows)
                or result["row_count"] >= data["execution_limit"],
            },
        }
    except httpx.TimeoutException:
        return {"status": "failed", "reason": "超时"}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {"status": "failed", "reason": "DTB 或服务错误"}


def _answer(client, question, args):
    try:
        with client.stream(
            "POST",
            "/api/v1/conversations/ask",
            json={
                "question": question.question,
                "agent": args.agent,
                "provider_id": args.provider_id,
                "model": args.model,
            },
        ) as response:
            if not response.is_success:
                return None
            event = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "done":
                    sql = json.loads(line[5:]).get("sql")
                    if isinstance(sql, list):
                        return sql[0] if sql and isinstance(sql[0], str) else None
                    return sql if isinstance(sql, str) and sql.strip() else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    return None


def run_cli(args):
    token = os.environ.get(args.token)
    if not token:
        raise SystemExit("指定的 token 环境变量为空或不存在")
    questions = load_questions(args.questions)
    if args.only:
        selected = set(args.only.split(","))
        if selected - {q.id for q in questions}:
            raise SystemExit("--only 包含未知题号")
        questions = [q for q in questions if q.id in selected]
    if (
        args.max_queries < 0
        or len(questions) * (1 if args.skip_reference else 2) > args.max_queries
    ):
        raise SystemExit("题目可能超过 --max-queries 上限")
    entries = []
    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=600,
    ) as client:
        for question in questions:
            sql = _answer(client, question, args)
            agent = _execute(client, sql) if sql else {"status": "no_sql"}
            reference = (
                _execute(client, question.reference_sql)
                if not args.skip_reference
                else {"status": "skipped"}
            )
            comparison = "uncomparable"
            if agent["status"] == reference["status"] == "succeeded":
                comparison = compare_results(agent["result"], reference["result"])
            agent.pop("result", None)
            reference.pop("result", None)
            entries.append(
                {
                    "id": question.id,
                    "agent_sql": bool(sql),
                    "agent": agent,
                    "reference": reference,
                    "comparison": comparison,
                }
            )
    with_sql = sum(e["agent_sql"] for e in entries)
    successful = sum(e["agent"]["status"] == "succeeded" for e in entries)
    comparable = sum(e["comparison"] in ("equal", "different") for e in entries)
    equal = sum(e["comparison"] == "equal" for e in entries)
    metrics = {
        "questions": len(entries),
        "no_sql": len(entries) - with_sql,
        "execution_success": successful,
        "execution_total": with_sql,
        "execution_success_rate": successful / with_sql if with_sql else None,
        "equal": equal,
        "comparable": comparable,
        "consistency_rate": equal / comparable if comparable else None,
        "uncomparable": len(entries) - comparable,
        "reference_failures": sum(
            e["reference"]["status"] == "failed" for e in entries
        ),
    }
    failures = dict(
        Counter(
            e["agent"]["reason"] for e in entries if e["agent"]["status"] == "failed"
        )
    )
    report = {"metrics": metrics, "failure_reasons": failures, "questions": entries}
    args.report.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# 取数正确率评测", "", "## 总体指标", ""]
    lines += [f"- {key}: {value}" for key, value in metrics.items()]
    lines += [
        "",
        "## 失败原因",
        "",
        *[f"- {k}: {v}" for k, v in failures.items()],
        "",
        "| 题号 | 有 SQL | agent 执行 | 参考执行 | 一致性 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for e in entries:
        lines.append(
            f"| {e['id']} | {e['agent_sql']} | {e['agent']} | {e['reference']} | {e['comparison']} |"
        )
    args.report.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0
