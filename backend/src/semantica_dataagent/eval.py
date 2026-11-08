"""Question-set evaluation for retrieval and online-agent modes."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import sqlglot
import yaml
from sqlglot import exp

from .agent_types import (
    CLARIFICATION,
    DONE,
    ERROR,
    TEXT_DELTA,
    AgentTurnRequest,
    ProviderConfig,
)

DIALECTS = ("hive", "spark", "doris")
CLARIFY_MARKERS = (
    "澄清",
    "请确认",
    "需要确认",
    "哪种口径",
    "哪个口径",
    "请选择",
    "请明确",
    "要看",
    "取决于",
)
_FILTER_TYPES = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Like,
    exp.In,
    exp.Between,
    exp.Is,
)


def compare_results(left: dict, right: dict) -> str:
    """Compare unordered result multisets, retaining column positions."""
    if left.get("truncated") or right.get("truncated"):
        return "uncomparable"
    if len(left["columns"]) != len(right["columns"]):
        return "different"
    left_rows, right_rows = left["rows"], right["rows"]
    if len(left_rows) != len(right_rows):
        return "different"

    def equal_value(first, second):
        if first is None or second is None:
            return first is None and second is None
        if isinstance(first, bool) or isinstance(second, bool):
            return type(first) is type(second) and first == second
        try:
            number_a, number_b = Decimal(str(first)), Decimal(str(second))
            if number_a.is_finite() and number_b.is_finite():
                return abs(number_a - number_b) <= Decimal("1e-6") * max(
                    abs(number_a), abs(number_b)
                )
        except InvalidOperation:
            pass
        return first == second

    candidates = [
        [
            index
            for index, row in enumerate(right_rows)
            if len(row) == len(left["columns"])
            and len(other) == len(left["columns"])
            and all(equal_value(a, b) for a, b in zip(other, row, strict=True))
        ]
        for other in left_rows
    ]
    assigned: dict[int, int] = {}
    for source in range(len(left_rows)):
        queue = [source]
        parents: dict[int, tuple[int, int] | None] = {source: None}
        visited_targets: set[int] = set()
        found = False
        for current in queue:
            # Prefer unused targets to keep repeated rows inexpensive.
            free = next((t for t in candidates[current] if t not in assigned), None)
            if free is not None:
                target = free
                while True:
                    assigned[target] = current
                    parent = parents[current]
                    if parent is None:
                        break
                    current, target = parent
                found = True
                break
            for target in candidates[current]:
                if target in visited_targets:
                    continue
                visited_targets.add(target)
                other = assigned[target]
                if other not in parents:
                    parents[other] = (current, target)
                    queue.append(other)
        if not found:
            return "different"
    return "equal"


@dataclass(slots=True)
class Question:
    id: str
    question: str
    expected_tables: list[str]
    expected_fields: list[str]
    caliber: str
    reference_sql: str
    notes: str = ""
    dialect: str = "hive"
    clarify: bool = False


def _required(mapping: dict[str, Any], key: str, where: str):
    value = mapping.get(key)
    if value is None or value == "" or value == []:
        raise ValueError(f"{where}.{key} is required")
    return value


def _question_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("questions")
    if not isinstance(payload, list):
        raise TypeError("question set must be a list or an object with 'questions'")
    return payload


def load_questions(path: str | Path, *, text: str | None = None) -> list[Question]:
    source = Path(path)
    raw = text if text is not None else source.read_text(encoding="utf-8")
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid question YAML: {exc}") from None
    rows = _question_rows(payload)
    if not rows:
        raise ValueError("question set is empty")
    questions: list[Question] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise TypeError(f"question #{index} must be an object")
        where = f"question #{index} ({row.get('id', 'no-id')})"
        question_id = str(_required(row, "id", where)).strip()
        if question_id in seen:
            raise ValueError(f"duplicate question id: {question_id}")
        seen.add(question_id)
        tables = _required(row, "expected_tables", where)
        fields = _required(row, "expected_fields", where)
        if not isinstance(tables, list) or not all(
            isinstance(item, str) and item.strip() for item in tables
        ):
            raise ValueError(f"{where}.expected_tables must be a list of names")
        if not isinstance(fields, list) or not all(
            isinstance(item, str) and item.strip() for item in fields
        ):
            raise ValueError(f"{where}.expected_fields must be a list of names")
        dialect = str(row.get("dialect") or "hive").lower()
        if dialect not in DIALECTS:
            raise ValueError(f"{where}.dialect must be one of {', '.join(DIALECTS)}")
        questions.append(
            Question(
                id=question_id,
                question=str(_required(row, "question", where)).strip(),
                expected_tables=[item.strip() for item in tables],
                expected_fields=[item.strip() for item in fields],
                caliber=str(_required(row, "caliber", where)).strip(),
                reference_sql=str(_required(row, "reference_sql", where)).strip(),
                notes=str(row.get("notes") or "").strip(),
                dialect=dialect,
                clarify=bool(row.get("clarify", False)),
            )
        )
    return questions


def _full_name(value: str) -> str:
    return value.strip().strip('`"').lower()


def hit_table(hit: dict[str, Any]) -> str | None:
    ref = str(hit.get("ref") or "")
    if ref.startswith("table:"):
        return _full_name(ref.split(":", 1)[1])
    if hit.get("type") == "table" or (
        hit.get("database") and hit.get("name") and not ref.startswith("field:")
    ):
        database = hit.get("database")
        name = hit.get("name")
        if database and name:
            return _full_name(f"{database}.{name}")
    return None


def table_recall(
    expected: list[str], hits: list[dict[str, Any]], top_k: int = 5
) -> tuple[float, list[str], list[str]]:
    expected_names = [_full_name(item) for item in expected]
    retrieved: list[str] = []
    for hit in hits[:top_k]:
        name = hit_table(hit) if isinstance(hit, dict) else None
        if name and name not in retrieved:
            retrieved.append(name)
    found = [name for name in expected_names if name in retrieved]
    missing = [item for item in expected if _full_name(item) not in retrieved]
    recall = len(found) / len(expected_names) if expected_names else 1.0
    return recall, retrieved, missing


def _table_aliases(tree) -> tuple[set[str], dict[str, str]]:
    tables: set[str] = set()
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = ".".join(part for part in (table.db, table.name) if part)
        if not name:
            continue
        name = _full_name(name)
        tables.add(name)
        aliases[name] = name
        alias = table.alias_or_name
        if alias:
            aliases[alias.lower()] = name
    return tables, aliases


def _columns(tree, aliases: dict[str, str]) -> set[tuple[str | None, str]]:
    found: set[tuple[str | None, str]] = set()
    for column in tree.find_all(exp.Column):
        name = column.name.lower()
        if not name:
            continue
        owner = column.table.lower() if column.table else None
        found.add((aliases.get(owner) if owner else None, name))
    return found


def _filter_requirements(tree) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for clause in list(tree.find_all(exp.Where)) + list(tree.find_all(exp.Having)):
        for node in clause.find_all(*_FILTER_TYPES):
            column = next(iter(node.find_all(exp.Column)), None)
            if column is None or not column.name:
                continue
            values = []
            for literal in node.find_all(exp.Literal):
                value = str(literal.this).strip()
                if value and value.lower() not in {item.lower() for item in values}:
                    values.append(value)
            requirements.append({"column": column.name.lower(), "values": values})
    return requirements


def sql_satisfies_caliber(question: Question, sql: str) -> bool:
    try:
        reference = sqlglot.parse_one(question.reference_sql, read=question.dialect)
        tree = sqlglot.parse_one(sql, read=question.dialect)
    except (sqlglot.errors.SqlglotError, ValueError):
        return False
    if tree is None or reference is None:
        return False
    filters = _filter_requirements(reference)
    tables, aliases = _table_aliases(tree)
    columns = _columns(tree, aliases)
    column_names = {name for _, name in columns}

    for expected in question.expected_tables:
        if _full_name(expected) not in tables:
            return False
    for expected in question.expected_fields:
        text = expected.strip().strip('`"')
        if "." in text:
            table, name = text.rsplit(".", 1)
            table = _full_name(table)
            name = name.lower()
            if table not in tables:
                return False
            if not any(
                field_name == name and (owner is None or owner == table)
                for owner, field_name in columns
            ):
                return False
        elif text.lower() not in column_names:
            return False

    answer_text = sql.casefold()
    for requirement in filters:
        if requirement["column"] not in column_names:
            return False
        for value in requirement["values"]:
            if value.casefold() not in answer_text:
                return False
    return True


def caliber_hit(
    question: Question, sqls: list[str] | None, answer_text: str = ""
) -> bool:
    if question.clarify and any(
        marker in (answer_text or "") for marker in CLARIFY_MARKERS
    ):
        return True
    return any(sql_satisfies_caliber(question, sql) for sql in (sqls or []) if sql)


def _validation_entry(result: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "status": "error",
            "valid": False,
            "error": f"invalid {label} result: {type(result).__name__}",
        }
    status = str(result.get("status") or "unknown")
    entry = {"status": status, "valid": status == "pass"}
    error = result.get("error")
    if error:
        entry["error"] = str(error)
    return entry


def score_question(
    question: Question,
    *,
    hits: list[dict[str, Any]] | None,
    validation: dict[str, Any] | None,
    answer_sqls: list[str] | None = None,
    answer_text: str = "",
    mode: Literal["retrieval", "agent"] = "retrieval",
    top_k: int = 5,
    retrieval_error: str | None = None,
    answer_error: str | None = None,
) -> dict[str, Any]:
    hits = hits or []
    recall, retrieved, missing = table_recall(question.expected_tables, hits, top_k)
    validation_entry = _validation_entry(validation, label="validate_sql")
    if validation is None and not answer_error and not retrieval_error:
        validation_entry = {
            "status": "missing",
            "valid": False,
            "error": "validate_sql was not run",
        }
    sqls = answer_sqls if answer_sqls is not None else [question.reference_sql]
    hit = caliber_hit(question, sqls, answer_text)
    reasons: list[str] = []
    categories: list[str] = []
    if retrieval_error:
        reasons.append(f"retrieval error: {retrieval_error}")
        categories.append("检索失败")
    if recall < 1.0:
        reasons.append(
            f"table recall@{top_k} {recall:.2f} < 1.00: missing {', '.join(missing)}"
        )
        categories.append("检索没召回")
    if answer_error:
        reasons.append(f"agent error: {answer_error}")
        categories.append("agent 失败")
    elif not validation_entry.get("valid"):
        if mode == "agent" and not sqls:
            reasons.append("validate_sql missing: agent produced no SQL")
            categories.append("agent 未给出 SQL")
        else:
            reasons.append(f"validate_sql status={validation_entry['status']}")
            categories.append("题目本身有问题（参考 SQL 未通过校验）")
    if mode == "agent" and not hit:
        reasons.append(
            "caliber miss: expected tables/fields/filters are not all present"
        )
        categories.append("口径未命中")
    return {
        "id": question.id,
        "question": question.question,
        "expected_tables": question.expected_tables,
        "expected_fields": question.expected_fields,
        "caliber": question.caliber,
        "reference_sql": question.reference_sql,
        "dialect": question.dialect,
        "clarify": question.clarify,
        "retrieved_tables": retrieved,
        "missing_tables": missing,
        "table_recall_at_5": recall,
        "validate_sql": validation_entry,
        "answer_sql": sqls if mode == "agent" else [],
        "answer_text": answer_text if mode == "agent" else "",
        "caliber_hit": hit,
        "failure_reasons": reasons,
        "failure_categories": categories,
        "notes": question.notes,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _source_name(source: str | Path | None) -> str | None:
    return Path(source).name if source else None


def run_evaluation(
    questions: list[Question],
    *,
    retrieve: Callable[[str], list[dict[str, Any]]],
    validate: Callable[[str, str], dict[str, Any]],
    top_k: int = 5,
    mode: Literal["retrieval", "agent"] = "retrieval",
    answers: Callable[[Question], list[str]] | None = None,
    answer_texts: Callable[[Question], str] | None = None,
    answer_errors: Callable[[Question], str | None] | None = None,
    source: str | Path | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for question in questions:
        started = time.monotonic()
        hits: list[dict[str, Any]] = []
        validation: dict[str, Any] | None = None
        retrieval_error = None
        try:
            hits = retrieve(question.question) or []
        except Exception as exc:  # noqa: BLE001 - evaluation reports, it does not crash
            retrieval_error = f"{type(exc).__name__}: {exc}"
        answer_sqls: list[str] | None = None
        answer_text = ""
        answer_error = None
        if mode == "agent":
            try:
                answer_sqls = (answers(question) if answers else []) or []
            except Exception as exc:  # noqa: BLE001
                answer_sqls = []
                answer_error = f"{type(exc).__name__}: {exc}"
            answer_text = answer_texts(question) if answer_texts else ""
            if answer_errors:
                answer_error = answer_errors(question) or answer_error
        target_sql = (
            question.reference_sql
            if mode == "retrieval"
            else (answer_sqls[0] if answer_sqls else None)
        )
        if target_sql and not answer_error:
            try:
                validation = validate(target_sql, question.dialect)
            except Exception as exc:  # noqa: BLE001
                validation = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        result = score_question(
            question,
            hits=hits,
            validation=validation,
            answer_sqls=answer_sqls,
            answer_text=answer_text,
            mode=mode,
            top_k=top_k,
            retrieval_error=retrieval_error,
            answer_error=answer_error,
        )
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        results.append(result)

    recalls = [row["table_recall_at_5"] for row in results]
    validations = [1.0 if row["validate_sql"].get("valid") else 0.0 for row in results]
    caliber = [1.0 if row["caliber_hit"] else 0.0 for row in results]
    metrics: dict[str, Any] = {
        "questions": len(results),
        "table_recall_at_5": _mean(recalls),
        "table_hit_rate_at_5": _mean(
            [1.0 if value == 1.0 else 0.0 for value in recalls]
        ),
        "validate_sql_pass_rate": _mean(validations),
        "caliber_hit_rate": _mean(caliber) if mode == "agent" else None,
        "reference_sql_caliber_hit_rate": _mean(caliber)
        if mode == "retrieval"
        else None,
        "retrieval_errors": sum(
            1 for row in results if "检索失败" in row["failure_categories"]
        ),
        "agent_errors": sum(
            1 for row in results if "agent 失败" in row["failure_categories"]
        ),
        "below_threshold": _mean(recalls) < 0.8,
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "source": _source_name(source),
        "top_k": top_k,
        "scoring": {
            "table_recall_at_5": (
                "Mean over questions of |expected tables intersected with the first "
                f"{top_k} table hits| / |expected tables|; duplicate hits count once."
            ),
            "validate_sql_pass_rate": (
                "Fraction of questions whose checked SQL returns validate_sql status=pass. "
                "Retrieval mode checks reference_sql; agent mode checks the agent answer SQL."
            ),
            "caliber_hit_rate": (
                "Agent mode only. A hit requires the answer SQL to reference every expected "
                "table and field and every column/literal filter present in reference_sql. "
                "Questions marked clarify can instead hit by asking for clarification."
                if mode == "agent"
                else None
            ),
            "reference_sql_caliber_hit_rate": (
                "Retrieval mode self-check: the same caliber rule applied to reference_sql. "
                "It is reported separately and is not used as the agent caliber metric."
                if mode == "retrieval"
                else None
            ),
        },
        "metrics": metrics,
        "results": results,
    }


def _fmt_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _status(value: bool) -> str:
    return "通过" if value else "失败"


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# 评测报告",
        "",
        f"- 生成时间：{report.get('generated_at', '')}",
        f"- 模式：{report.get('mode')}",
        f"- 题集：{report.get('source') or ''}",
        f"- 前 N 条：{report.get('top_k')}",
        "",
        "## 总体指标",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 题数 | {metrics['questions']} |",
        f"| 表召回率@5 | {_fmt_rate(metrics['table_recall_at_5'])} |",
        f"| 表全命中题占比@5 | {_fmt_rate(metrics['table_hit_rate_at_5'])} |",
        f"| validate_sql 通过率 | {_fmt_rate(metrics['validate_sql_pass_rate'])} |",
        f"| 口径命中率 | {_fmt_rate(metrics.get('caliber_hit_rate'))} |",
    ]
    if metrics.get("reference_sql_caliber_hit_rate") is not None:
        lines.append(
            f"| 参考 SQL 口径自检率 | {_fmt_rate(metrics['reference_sql_caliber_hit_rate'])} |"
        )
    lines += [
        f"| 检索错误题数 | {metrics.get('retrieval_errors', 0)} |",
        f"| Agent 错误题数 | {metrics.get('agent_errors', 0)} |",
        "",
        "## 判定方法",
        "",
    ]
    for key, description in report["scoring"].items():
        if description:
            lines.append(f"- `{key}`：{description}")
    lines += [
        "",
        "## 逐题结果",
        "",
        "| 题号 | 表召回率@5 | validate_sql | 口径命中 | 失败原因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["results"]:
        reasons = "；".join(row["failure_reasons"]) or "—"
        lines.append(
            f"| {row['id']} | {row['table_recall_at_5']:.2f} | "
            f"{_status(row['validate_sql'].get('valid', False))} | "
            f"{'—' if report['mode'] != 'agent' else _status(row['caliber_hit'])} | "
            f"{reasons} |"
        )
    lines += [
        "",
        "## 失败原因",
        "",
    ]
    failed = [row for row in report["results"] if row["failure_reasons"]]
    if not failed:
        lines.append("无。")
    else:
        for row in failed:
            lines.append(f"### {row['id']}：{row['question']}")
            lines.append("")
            lines.append(f"- 期望表：{', '.join(row['expected_tables'])}")
            lines.append(f"- 实际召回：{', '.join(row['retrieved_tables']) or '无'}")
            lines.append(f"- 期望字段：{', '.join(row['expected_fields'])}")
            lines.append(f"- 口径：{row['caliber']}")
            if row["notes"]:
                lines.append(f"- 出处：{row['notes']}")
            for reason in row["failure_reasons"]:
                lines.append(f"- {reason}")
            categories = row.get("failure_categories") or []
            if categories:
                lines.append(f"- 归类：{'；'.join(categories)}")
            lines.append("")
    if metrics.get("below_threshold"):
        lines += [
            "## 表召回率未达标",
            "",
            "表召回率@5 低于 80%，以下逐题列出原因，未修改题目或检索逻辑：",
            "",
        ]
        for row in report["results"]:
            if row["table_recall_at_5"] >= 1.0:
                continue
            lines.append(
                f"- {row['id']}：{'；'.join(row['failure_reasons']) or '未知'}"
            )
    return "\n".join(lines).rstrip() + "\n"


def _report_paths(prefix: str | Path) -> tuple[Path, Path]:
    path = Path(prefix)
    if path.suffix == ".md":
        return path, path.with_suffix(".json")
    if path.suffix == ".json":
        return path.with_suffix(".md"), path
    return Path(str(path) + ".md"), Path(str(path) + ".json")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_reports(report: dict[str, Any], prefix: str | Path) -> tuple[Path, Path]:
    markdown, payload = _report_paths(prefix)
    _atomic_write(
        payload, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    _atomic_write(markdown, render_markdown(report))
    return markdown, payload


async def collect_agent_answers(
    questions: list[Question],
    *,
    agent: str,
    model: str,
    provider: ProviderConfig,
    mcp_url: str,
    mcp_token: str,
    workspace: str | Path,
    timeout: float = 600.0,
) -> tuple[
    dict[str, list[str]], dict[str, str], dict[str, str | None], dict[str, bool]
]:
    from .agent_runner import get_runner

    runner = get_runner(agent)
    if runner is None:
        raise ValueError(f"unknown agent: {agent}")
    if not runner.available():
        raise RuntimeError(f"agent runtime is not installed: {agent}")
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    answers: dict[str, list[str]] = {}
    texts: dict[str, str] = {}
    errors: dict[str, str | None] = {}
    clarifications: dict[str, bool] = {}
    for question in questions:
        conversation_id = "eval-" + uuid.uuid4().hex
        request = AgentTurnRequest(
            conversation_id=conversation_id,
            prompt=question.question,
            session_id=conversation_id,
            workspace=root / conversation_id,
            provider=provider,
            model=model,
            mcp_url=mcp_url,
            mcp_token=mcp_token,
        )
        sql: list[str] = []
        text_parts: list[str] = []
        final_text = ""
        error: str | None = None
        clarified = False
        try:
            async with asyncio.timeout(timeout):
                async for event in runner.run(request):
                    if event.type == TEXT_DELTA:
                        text_parts.append(str(event.data.get("text") or ""))
                    elif event.type == DONE:
                        final_text = str(event.data.get("text") or "")
                        for item in event.data.get("sql") or []:
                            if isinstance(item, dict) and item.get("sql"):
                                sql.append(str(item["sql"]))
                    elif event.type == CLARIFICATION:
                        clarified = True
                    elif event.type == ERROR:
                        error = str(event.data.get("message") or "agent error")
        except TimeoutError:
            error = f"agent timed out after {timeout:.0f}s"
        except Exception as exc:  # noqa: BLE001 - one answer must not stop the set
            error = f"{type(exc).__name__}: {exc}"
        answers[question.id] = sql
        texts[question.id] = final_text or "".join(text_parts)
        errors[question.id] = error
        clarifications[question.id] = clarified
    return answers, texts, errors, clarifications


def default_mcp_url() -> str:
    value = os.environ.get("DATAAGENT_EVAL_MCP_URL") or os.environ.get(
        "DATAAGENT_PUBLIC_URL"
    )
    if value:
        return value.rstrip("/") + (
            "" if value.rstrip("/").endswith("/mcp") else "/mcp/"
        )
    return "http://127.0.0.1:8000/mcp/"


def default_provider_url() -> str:
    return (
        os.environ.get("DATAAGENT_EVAL_PROVIDER_URL")
        or os.environ.get("TEST_PI_BASE_URL")
        or "http://127.0.0.1:3425"
    )


def provider_from_environment(model: str) -> ProviderConfig:
    return ProviderConfig(
        id="eval",
        name="eval",
        type=os.environ.get("DATAAGENT_EVAL_PROVIDER_TYPE", "anthropic-messages"),
        base_url=default_provider_url(),
        models=[model],
        api_key=os.environ.get("DATAAGENT_EVAL_PROVIDER_API_KEY"),
    )


def temporary_token() -> tuple[str, str]:
    """Create a short-lived user token so the agent can call this MCP server."""
    import secrets

    from .db import Token, digest, sessions
    from .project_api import ensure_default_project_member

    plaintext = secrets.token_urlsafe(32)
    with sessions().begin() as db:
        row = Token(
            digest=digest(plaintext),
            name=f"eval-{uuid.uuid4().hex[:8]}",
            role="user",
        )
        db.add(row)
        db.flush()
        ensure_default_project_member(db, row)
        return plaintext, row.id


def remove_token(token_id: str) -> None:
    from .db import Token, sessions

    with sessions().begin() as db:
        row = db.get(Token, token_id)
        if row is not None:
            db.delete(row)


def cleanup_workspace(path: str | Path) -> None:
    import shutil

    with suppress(FileNotFoundError):
        shutil.rmtree(path)
