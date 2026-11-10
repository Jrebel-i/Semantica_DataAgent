"""Evaluate agent SQL through the application's query interfaces."""

import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx
import sqlglot
import yaml
from sqlglot import exp

from .eval import (
    DEFAULT_CATEGORY,
    FAILURE_CATEGORIES,
    _atomic_write,
    _report_paths,
    compare_results,
    load_questions,
)

DETAIL_LIMIT = 200
CLARIFY_MESSAGE = "请按以下口径回答并给出 SQL：{caliber}"
DEFAULT_EXPECTED_CATEGORIES = (
    "维度值筛选",
    "时间语义",
    "口径歧义",
    "合法空结果",
    "多表 Join",
)


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
        "tool_calls": [],
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
                    elif event == "tool_start":
                        name = data.get("name")
                        if isinstance(name, str) and name:
                            item = {"name": name}
                            if data.get("summary"):
                                item["summary"] = data["summary"]
                            result["tool_calls"].append(item)
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
    tool_calls = list(first["tool_calls"])
    if first["sql"]:
        return {
            "sql": first["sql"],
            "clarified": False,
            "via_plan": first["via_plan"],
            "conversation_id": first["conversation_id"],
            "tool_calls": tool_calls,
        }
    conversation_id = first["conversation_id"]
    if not first["clarification"] or not conversation_id:
        return {
            "sql": None,
            "clarified": False,
            "via_plan": False,
            "conversation_id": conversation_id,
            "tool_calls": tool_calls,
        }
    second = _ask(
        client,
        {
            "question": CLARIFY_MESSAGE.format(caliber=question.caliber),
            "conversation_id": conversation_id,
        },
        project=getattr(args, "project", None),
    )
    tool_calls.extend(second["tool_calls"])
    return {
        "sql": second["sql"],
        "clarified": True,
        "via_plan": second["via_plan"],
        "conversation_id": conversation_id,
        "tool_calls": tool_calls,
    }


def _summary(comparison):
    if comparison == "equal":
        return "行数、列数及结果一致"
    if comparison == "different":
        return "行数、列数或结果不同"
    return None


def _expected_categories(value):
    if value is None:
        return list(DEFAULT_EXPECTED_CATEGORIES)
    items = value.split(",") if isinstance(value, str) else list(value)
    categories = []
    for item in items:
        category = str(item).strip()
        if category and category not in categories:
            categories.append(category)
    return categories


def _verdict(runs):
    comparable_runs = sum(run["comparison"] in ("equal", "different") for run in runs)
    if comparable_runs * 2 <= len(runs):
        return "uncomparable", comparable_runs
    equal_runs = sum(run["comparison"] == "equal" for run in runs)
    if equal_runs * 2 > comparable_runs:
        return "correct", comparable_runs
    if equal_runs * 2 == comparable_runs:
        return "tie", comparable_runs
    return "wrong", comparable_runs


def _majority_comparison(result):
    if result == "correct":
        return "equal"
    if result == "uncomparable":
        return "uncomparable"
    return "different"


def _display_run(runs, comparison):
    return next((run for run in runs if run["comparison"] == comparison), runs[0])


def _uncomparable_reason(result, reference):
    if result != "uncomparable":
        return None
    if reference.get("status") == "failed":
        return "reference_failed"
    if reference.get("status") == "skipped":
        return "reference_skipped"
    return "majority_uncomparable"


def _coverage(entries):
    coverage = {}
    for entry in entries:
        category = entry.get("category") or DEFAULT_CATEGORY
        item = coverage.setdefault(
            category,
            {
                "total": 0,
                "correct": 0,
                "wrong": 0,
                "tie": 0,
                "uncomparable": 0,
                "volatile": 0,
                "failure_categories": {},
            },
        )
        item["total"] += 1
        item[entry["result"]] += 1
        if entry["volatile"]:
            item["volatile"] += 1
        failure_category = entry.get("failure_category")
        if failure_category:
            item["failure_categories"][failure_category] = (
                item["failure_categories"].get(failure_category, 0) + 1
            )
    return dict(sorted(coverage.items()))


def _failure_summary(entries):
    counts = Counter(
        entry["failure_category"] for entry in entries if entry.get("failure_category")
    )
    return dict(sorted(counts.items()))


def _recompute_summaries(report):
    entries = report.get("questions") or []
    report["failure_categories"] = _failure_summary(entries)
    report["coverage"] = _coverage(entries)
    expected = _expected_categories(report.get("expected_categories"))
    report["expected_categories"] = expected
    coverage = report["coverage"]
    report["coverage_gaps"] = [
        category
        for category in expected
        if coverage.get(category, {}).get("total", 0) == 0
    ]
    metrics = report.setdefault("metrics", {})
    classified = sum(bool(entry.get("failure_category")) for entry in entries)
    metrics["classified"] = classified
    metrics["unclassified"] = len(entries) - classified
    return report


def render_report(report):
    metrics = report.get("metrics") or {}
    entries = report.get("questions") or []
    lines = [
        "# 取数正确率评测",
        "",
        f"- split: {report.get('split', 'dev')}",
        f"- repeat: {report.get('repeat', 1)}",
        "",
        "## 总体指标",
        "",
    ]
    lines += [f"- {key}: {value}" for key, value in metrics.items()]
    lines += ["", "## 未确认答案（不参与评测）", ""]
    excluded = report.get("excluded_unconfirmed") or []
    if excluded:
        lines += [f"- {question_id}" for question_id in excluded]
    else:
        lines.append("无。")
    lines += ["", "## 不可比题目（不计入一致率分母）", ""]
    uncomparable = report.get("excluded_uncomparable") or []
    if uncomparable:
        lines += [
            "| 题号 | 原因 | 参考 SQL 状态 | 可比较遍数 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {item['id']} | {item['reason']} | {item.get('reference_status') or '—'} | "
            f"{item.get('comparable_runs', 0)}/{item.get('run_count', 0)} |"
            for item in uncomparable
        ]
    else:
        lines.append("无。")
    lines += ["", "## 失败原因", ""]
    failures = report.get("failure_reasons") or {}
    lines += [f"- {key}: {value}" for key, value in failures.items()]
    if not failures:
        lines.append("无。")
    lines += ["", "## 失败分类汇总", ""]
    categories = report.get("failure_categories") or {}
    if categories:
        lines += ["| 分类 | 题数 |", "| --- | --- |"]
        lines += [f"| {key} | {value} |" for key, value in categories.items()]
    else:
        lines.append(
            "尚未填写。用 `eval-report --report <报告前缀> --categories <分类文件>` 合并。"
        )
    lines += ["", "## 覆盖情况", ""]
    coverage = report.get("coverage") or {}
    if coverage:
        lines += [
            "| category | 题数 | 答对 | 答错 | 平局 | 不可比 | 波动 | 失败分类 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for category, item in coverage.items():
            detail = ", ".join(
                f"{key}: {value}"
                for key, value in item.get("failure_categories", {}).items()
            )
            lines.append(
                f"| {_cell(category)} | {item['total']} | {item['correct']} | "
                f"{item['wrong']} | {item['tie']} | {item.get('uncomparable', 0)} | "
                f"{item['volatile']} | {_cell(detail) or '—'} |"
            )
    else:
        lines.append("无已评测题目。")
    lines += ["", "## 覆盖缺口", ""]
    expected = report.get("expected_categories") or []
    if expected:
        lines += [f"- 期望题型：{', '.join(expected)}"]
    gaps = report.get("coverage_gaps") or []
    lines += [f"- 缺失题型：{', '.join(gaps) if gaps else '无'}"]
    lines += [
        "",
        (
            "| 题号 | 有 SQL | 澄清 | 查询计划 | agent 执行 | 参考执行 | 一致性 | "
            "缺失条件列 | agent 列数 | 参考列数 | agent detail | 参考 detail |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        missing = entry["missing_condition_columns"]
        missing_text = "—" if missing is None else ", ".join(missing) or "—"
        lines.append(
            f"| {entry['id']} | {entry['agent_sql']} | {entry['clarified']} | "
            f"{entry['via_plan']} | {entry['agent']} | {entry['reference']} | "
            f"{entry['comparison']} | {_cell(missing_text)} | "
            f"{entry['agent_column_count'] if entry['agent_column_count'] is not None else '—'} | "
            f"{entry['reference_column_count'] if entry['reference_column_count'] is not None else '—'} | "
            f"{_cell(entry['detail'])} | "
            f"{_cell(entry['reference_detail'])} |"
        )
    lines += ["", "## 逐题明细", ""]
    for entry in entries:
        lines += [
            f"### {entry['id']}（{entry.get('category') or DEFAULT_CATEGORY} · {entry.get('split', 'dev')}）",
            "",
            f"- 问题：{_cell(entry.get('question'))}",
            f"- 参考 SQL：`{_cell(entry.get('reference_sql'))}`",
            (
                f"- 判定：{entry['result']}（{entry['equal_runs']}/{entry['run_count']} 遍一致，"
                f"{entry.get('comparable_runs', 0)} 遍可比较）"
            ),
            f"- 波动题：{'是' if entry['volatile'] else '否'}",
            f"- failure_category：{entry.get('failure_category') or '未填写'}",
        ]
        if entry.get("uncomparable_reason"):
            lines.append(f"- 不可比原因：{entry['uncomparable_reason']}")
        for run in entry["runs"]:
            calls = " → ".join(
                f"{call.get('name')}({call.get('summary')})"
                if call.get("summary")
                else str(call.get("name"))
                for call in run["tool_calls"]
            )
            lines += [
                "",
                f"#### {entry['id']} 第 {run['run']} 遍",
                "",
                f"- 对话：`{run.get('conversation_id') or '—'}`",
                f"- 实际 SQL：`{_cell(run.get('sql')) or '—'}`",
                f"- 参考 SQL：`{_cell(run.get('reference_sql'))}`",
                f"- 工具调用：{calls or '无'}",
                f"- 一致性：{run['comparison']}",
            ]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(prefix, report):
    markdown, payload = _report_paths(prefix)
    _atomic_write(
        payload, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    _atomic_write(markdown, render_report(report))
    return markdown, payload


def _load_categories(path):
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"无法读取分类文件：{exc}") from None
    mapping = {}
    if isinstance(payload, dict):
        items = payload.items()
    elif isinstance(payload, list):
        items = []
        for row in payload:
            if not isinstance(row, dict) or not row.get("id"):
                raise SystemExit("分类文件里的每一项都必须包含 id")
            items.append((row["id"], row.get("failure_category")))
    else:
        raise SystemExit("分类文件必须是 id 到分类的映射或对象列表")
    for question_id, category in items:
        if category in (None, ""):
            mapping[str(question_id)] = None
            continue
        if not isinstance(category, str) or category not in FAILURE_CATEGORIES:
            raise SystemExit(f"未知失败分类：{category}")
        mapping[str(question_id)] = category
    return mapping


def run_report(args):
    _, payload_path = _report_paths(args.report)
    try:
        report = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"无法读取评测报告：{exc}") from None
    if not isinstance(report, dict) or not isinstance(report.get("questions"), list):
        raise SystemExit("评测报告缺少 questions")
    mapping = _load_categories(args.categories)
    known = {str(entry.get("id")) for entry in report["questions"]}
    unknown = sorted(set(mapping) - known)
    if unknown:
        raise SystemExit(f"分类文件包含未知题号：{', '.join(unknown)}")
    for entry in report["questions"]:
        question_id = str(entry.get("id"))
        if question_id in mapping:
            entry["failure_category"] = mapping[question_id]
    _recompute_summaries(report)
    write_report(args.report, report)
    return 0


def run_cli(args):
    token = os.environ.get(args.token)
    if not token:
        raise SystemExit("指定的 token 环境变量为空或不存在")
    questions = load_questions(args.questions)
    repeat = getattr(args, "repeat", 1)
    if not isinstance(repeat, int) or repeat < 1:
        raise SystemExit("--repeat 必须是大于等于 1 的整数")
    split = getattr(args, "split", "dev") or "dev"
    if split not in ("dev", "holdout", "all"):
        raise SystemExit("--split 只能是 dev、holdout 或 all")
    expected_categories = _expected_categories(
        getattr(args, "expected_categories", None)
    )
    unconfirmed = [question for question in questions if not question.answer_confirmed]
    confirmed = [question for question in questions if question.answer_confirmed]
    selected = (
        confirmed if split == "all" else [q for q in confirmed if q.split == split]
    )
    if args.only:
        only = set(args.only.split(","))
        available = {question.id for question in selected}
        missing = only - available
        if missing:
            if missing <= {question.id for question in questions}:
                raise SystemExit("--only 的题号不在所选 split 或已被排除")
            raise SystemExit("--only 包含未知题号")
        selected = [question for question in selected if question.id in only]
    needed = len(selected) * (
        repeat + (0 if getattr(args, "skip_reference", False) else 1)
    )
    if args.max_queries < 0 or needed > args.max_queries:
        raise SystemExit("题目可能超过 --max-queries 上限")
    entries = []
    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=600,
    ) as client:
        for question in selected:
            reference = (
                _execute(client, question.reference_sql)
                if not getattr(args, "skip_reference", False)
                else {"status": "skipped"}
            )
            reference_truncated = bool(
                reference.get("result", {}).get("truncated", False)
            )
            runs = []
            for run_number in range(1, repeat + 1):
                answer = _answer(client, question, args)
                sql = answer["sql"]
                agent = (
                    _execute(client, sql)
                    if sql
                    else {"status": "no_sql", "detail": None}
                )
                comparison = "uncomparable"
                if agent["status"] == reference["status"] == "succeeded":
                    comparison = compare_results(agent["result"], reference["result"])
                truncated = (
                    bool(agent.get("result", {}).get("truncated", False))
                    or reference_truncated
                )
                missing = missing_condition_columns(
                    question.reference_sql, sql, question.dialect
                )
                record = {
                    "run": run_number,
                    "conversation_id": answer["conversation_id"],
                    "tool_calls": answer["tool_calls"],
                    "tool_call_sequence": [
                        call["name"] for call in answer["tool_calls"]
                    ],
                    "sql": sql,
                    "actual_sql": sql,
                    "agent_sql": bool(sql),
                    "clarified": answer["clarified"],
                    "via_plan": answer["via_plan"],
                    "agent": agent,
                    "reference_status": reference["status"],
                    "reference_sql": question.reference_sql,
                    "detail": agent.get("detail"),
                    "comparison": comparison,
                    "consistency": comparison,
                    "truncated": truncated,
                    "summary": _summary(comparison),
                    "missing_condition_columns": missing,
                    "agent_column_count": agent.get("column_count"),
                    "reference_column_count": reference.get("column_count"),
                }
                agent.pop("result", None)
                runs.append(record)
            reference.pop("result", None)
            equal_runs = sum(run["comparison"] == "equal" for run in runs)
            result, comparable_runs = _verdict(runs)
            comparison = _majority_comparison(result)
            display = _display_run(runs, comparison)
            comparisons = dict(Counter(run["comparison"] for run in runs))
            clarified_runs = sum(run["clarified"] for run in runs)
            via_plan_runs = sum(run["via_plan"] for run in runs)
            no_sql_runs = sum(not run["agent_sql"] for run in runs)
            condition_incomplete_runs = sum(
                bool(run["missing_condition_columns"]) for run in runs
            )
            entries.append(
                {
                    "id": question.id,
                    "question": question.question,
                    "category": question.category,
                    "split": question.split,
                    "answer_confirmed": question.answer_confirmed,
                    "reference_sql": question.reference_sql,
                    "sql": display["sql"],
                    "actual_sql": display["sql"],
                    "agent_sql": display["agent_sql"],
                    "conversation_id": display["conversation_id"],
                    "tool_calls": display["tool_calls"],
                    "tool_call_sequence": display["tool_call_sequence"],
                    "clarified": clarified_runs > 0,
                    "clarified_runs": clarified_runs,
                    "via_plan": via_plan_runs > 0,
                    "via_plan_runs": via_plan_runs,
                    "no_sql": no_sql_runs > 0,
                    "no_sql_runs": no_sql_runs,
                    "condition_incomplete": condition_incomplete_runs > 0,
                    "condition_incomplete_runs": condition_incomplete_runs,
                    "detail": display["detail"],
                    "reference_detail": reference.get("detail"),
                    "agent": display["agent"],
                    "reference": reference,
                    "comparison": comparison,
                    "consistency": comparison,
                    "truncated": any(run["truncated"] for run in runs),
                    "summary": _summary(comparison),
                    "missing_condition_columns": display["missing_condition_columns"],
                    "agent_column_count": display["agent_column_count"],
                    "reference_column_count": display["reference_column_count"],
                    "runs": runs,
                    "run_count": repeat,
                    "equal_runs": equal_runs,
                    "comparable_runs": comparable_runs,
                    "comparison_counts": comparisons,
                    "result": result,
                    "majority_result": result,
                    "correct": result == "correct",
                    "volatile": len(comparisons) > 1,
                    "uncomparable_reason": _uncomparable_reason(result, reference),
                    "failure_category": None,
                }
            )
    comparable = sum(entry["result"] != "uncomparable" for entry in entries)
    equal = sum(entry["result"] == "correct" for entry in entries)
    execution_total = sum(
        run["agent_sql"] for entry in entries for run in entry["runs"]
    )
    execution_success = sum(
        run["agent"]["status"] == "succeeded"
        for entry in entries
        for run in entry["runs"]
    )
    metrics = {
        "questions": len(entries),
        "runs": sum(entry["run_count"] for entry in entries),
        "no_sql": sum(entry["no_sql"] for entry in entries),
        "no_sql_runs": sum(entry["no_sql_runs"] for entry in entries),
        "clarified": sum(entry["clarified"] for entry in entries),
        "clarified_runs": sum(entry["clarified_runs"] for entry in entries),
        "via_plan": sum(entry["via_plan"] for entry in entries),
        "via_plan_runs": sum(entry["via_plan_runs"] for entry in entries),
        "execution_success": execution_success,
        "execution_total": execution_total,
        "execution_success_rate": (
            execution_success / execution_total if execution_total else None
        ),
        "equal": equal,
        "comparable": comparable,
        "consistency_rate": equal / comparable if comparable else None,
        "uncomparable": len(entries) - comparable,
        "truncated": sum(entry["truncated"] for entry in entries),
        "reference_failures": sum(
            entry["reference"]["status"] == "failed" for entry in entries
        ),
        "condition_incomplete": sum(entry["condition_incomplete"] for entry in entries),
        "condition_incomplete_runs": sum(
            entry["condition_incomplete_runs"] for entry in entries
        ),
        "correct": equal,
        "wrong": sum(entry["result"] == "wrong" for entry in entries),
        "tie": sum(entry["result"] == "tie" for entry in entries),
        "volatile": sum(entry["volatile"] for entry in entries),
    }
    failures = dict(
        Counter(
            entry["agent"]["reason"]
            for entry in entries
            if entry["agent"]["status"] == "failed"
        )
    )
    run_failures = dict(
        Counter(
            run["agent"]["reason"]
            for entry in entries
            for run in entry["runs"]
            if run["agent"]["status"] == "failed"
        )
    )
    excluded = [
        {
            "id": question.id,
            "category": question.category,
            "split": question.split,
            "reason": "answer_confirmed=false",
        }
        for question in unconfirmed
    ]
    excluded_uncomparable = [
        {
            "id": entry["id"],
            "category": entry["category"],
            "split": entry["split"],
            "reason": entry["uncomparable_reason"],
            "reference_status": entry["reference"]["status"],
            "comparable_runs": entry["comparable_runs"],
            "run_count": entry["run_count"],
        }
        for entry in entries
        if entry["result"] == "uncomparable"
    ]
    report = {
        "project": getattr(args, "project", None),
        "split": split,
        "repeat": repeat,
        "expected_categories": expected_categories,
        "metrics": metrics,
        "failure_reasons": failures,
        "run_failure_reasons": run_failures,
        "excluded_unconfirmed": [question.id for question in unconfirmed],
        "excluded": excluded,
        "excluded_uncomparable": excluded_uncomparable,
        "questions": entries,
    }
    _recompute_summaries(report)
    write_report(args.report, report)
    return 0
