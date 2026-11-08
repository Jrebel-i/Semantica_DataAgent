"""Evaluate agent SQL through the application's query interfaces."""

import json
import os
import time
from collections import Counter

import httpx
import sqlglot
from sqlglot import exp

from .eval import compare_results, load_questions

DETAIL_LIMIT = 200
CLARIFY_MESSAGE = "请按以下口径回答并给出 SQL：{caliber}"


def where_columns(sql, dialect="hive"):
    """Column names used by WHERE filters; None when the SQL cannot be parsed."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return None
    if tree is None:
        return None
    return {
        column.name.lower()
        for where in tree.find_all(exp.Where)
        for column in where.find_all(exp.Column)
        if column.name
    }


def missing_condition_columns(reference_sql, agent_sql, dialect="hive"):
    """Reference WHERE columns the agent SQL does not mention.

    This is a diagnostic only. It deliberately does not compare values or
    change result consistency.
    """
    if not agent_sql:
        return None
    reference = where_columns(reference_sql, dialect)
    agent = where_columns(agent_sql, dialect)
    if reference is None or agent is None:
        return None
    return sorted(reference - agent)


def _failure(status):
    if status in (401, 403):
        return "鉴权失败"
    if status in (408, 504):
        return "超时"
    if status in (400, 422):
        return "校验被拒"
    return "DTB 或服务错误"


def _detail(response):
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None
    value = payload.get("detail") if isinstance(payload, dict) else None
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[:DETAIL_LIMIT]


def _sql(value):
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, dict):
        return _sql(value.get("sql"))
    if isinstance(value, list):
        for item in value:
            found = _sql(item)
            if found:
                return found
    return None


def _cell(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _ask(client, payload, project=None):
    result = {
        "sql": None,
        "conversation_id": None,
        "clarification": False,
        "via_plan": False,
    }
    if project is not None:
        payload = {**payload, "project": project}
    try:
        with client.stream(
            "POST", "/api/v1/conversations/ask", json=payload
        ) as response:
            if not response.is_success:
                return result
            event = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if event == "conversation":
                        conversation_id = data.get("conversation_id")
                        if isinstance(conversation_id, str):
                            result["conversation_id"] = conversation_id
                    elif event == "clarification":
                        result["clarification"] = True
                    elif event == "query_plan":
                        planned = _sql(data.get("sql"))
                        if planned:
                            result["sql"] = planned
                            result["via_plan"] = True
                    elif event == "done":
                        answered = _sql(data.get("sql"))
                        if answered:
                            result["sql"] = answered
    except (httpx.HTTPError, ValueError, TypeError):
        return result
    return result


def _execute(client, sql):
    started = time.monotonic()

    def failure(reason, detail=None):
        return {
            "status": "failed",
            "reason": reason,
            "detail": detail,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    try:
        plan = client.post("/api/v1/queries/plan", json={"sql": sql})
        if not plan.is_success:
            return failure(_failure(plan.status_code), _detail(plan))
        data = plan.json()
        response = client.post(f"/api/v1/queries/{data['id']}/execute", json={})
        if not response.is_success:
            return failure(_failure(response.status_code), _detail(response))
        result = response.json()
        rows = result["rows"]
        return {
            "status": "succeeded",
            "detail": None,
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
        return failure("超时")
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return failure("DTB 或服务错误")


def _answer(client, question, args):
    first = _ask(
        client,
        {
            "question": question.question,
            "agent": args.agent,
            "provider_id": args.provider_id,
            "model": args.model,
        },
        project=getattr(args, "project", None),
    )
    if first["sql"]:
        return first["sql"], False, first["via_plan"]
    conversation_id = first["conversation_id"]
    if not first["clarification"] or not conversation_id:
        return None, False, False
    second = _ask(
        client,
        {
            "question": CLARIFY_MESSAGE.format(caliber=question.caliber),
            "conversation_id": conversation_id,
        },
        project=getattr(args, "project", None),
    )
    return second["sql"], True, second["via_plan"]


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
            sql, clarified, via_plan = _answer(client, question, args)
            agent = (
                _execute(client, sql) if sql else {"status": "no_sql", "detail": None}
            )
            reference = (
                _execute(client, question.reference_sql)
                if not args.skip_reference
                else {"status": "skipped"}
            )
            comparison = "uncomparable"
            if agent["status"] == reference["status"] == "succeeded":
                comparison = compare_results(agent["result"], reference["result"])
            truncated = any(
                item.get("result", {}).get("truncated", False)
                for item in (agent, reference)
            )
            summary = None
            if comparison == "equal":
                summary = "行数、列数及结果一致"
            elif comparison == "different":
                summary = "行数、列数或结果不同"
            agent.pop("result", None)
            reference.pop("result", None)
            entries.append(
                {
                    "id": question.id,
                    "agent_sql": bool(sql),
                    "clarified": clarified,
                    "via_plan": via_plan,
                    "detail": agent.get("detail"),
                    "reference_detail": reference.get("detail"),
                    "agent": agent,
                    "reference": reference,
                    "comparison": comparison,
                    "truncated": truncated,
                    "summary": summary,
                    "missing_condition_columns": missing_condition_columns(
                        question.reference_sql, sql, question.dialect
                    ),
                    "agent_column_count": agent.get("column_count"),
                    "reference_column_count": reference.get("column_count"),
                }
            )
    with_sql = sum(e["agent_sql"] for e in entries)
    successful = sum(e["agent"]["status"] == "succeeded" for e in entries)
    comparable = sum(e["comparison"] in ("equal", "different") for e in entries)
    equal = sum(e["comparison"] == "equal" for e in entries)
    metrics = {
        "questions": len(entries),
        "no_sql": len(entries) - with_sql,
        "clarified": sum(e["clarified"] for e in entries),
        "via_plan": sum(e["via_plan"] for e in entries),
        "execution_success": successful,
        "execution_total": with_sql,
        "execution_success_rate": successful / with_sql if with_sql else None,
        "equal": equal,
        "comparable": comparable,
        "consistency_rate": equal / comparable if comparable else None,
        "uncomparable": len(entries) - comparable,
        "truncated": sum(e["truncated"] for e in entries),
        "reference_failures": sum(
            e["reference"]["status"] == "failed" for e in entries
        ),
        "condition_incomplete": sum(
            bool(e["missing_condition_columns"]) for e in entries
        ),
    }
    failures = dict(
        Counter(
            e["agent"]["reason"] for e in entries if e["agent"]["status"] == "failed"
        )
    )
    report = {
        "project": getattr(args, "project", None),
        "metrics": metrics,
        "failure_reasons": failures,
        "questions": entries,
    }
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
        (
            "| 题号 | 有 SQL | 澄清 | 查询计划 | agent 执行 | 参考执行 | 一致性 | "
            "缺失条件列 | agent 列数 | 参考列数 | agent detail | 参考 detail |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in entries:
        missing = e["missing_condition_columns"]
        missing_text = "—" if missing is None else ", ".join(missing) or "—"
        lines.append(
            f"| {e['id']} | {e['agent_sql']} | {e['clarified']} | "
            f"{e['via_plan']} | {e['agent']} | {e['reference']} | "
            f"{e['comparison']} | {_cell(missing_text)} | "
            f"{e['agent_column_count'] if e['agent_column_count'] is not None else '—'} | "
            f"{e['reference_column_count'] if e['reference_column_count'] is not None else '—'} | "
            f"{_cell(e['detail'])} | "
            f"{_cell(e['reference_detail'])} |"
        )
    args.report.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0
