"""System-generated table samples through the configured query executor."""

import asyncio
import time
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import HTTPException
from pydantic import Field
from sqlglot import exp

from . import knowledge
from .auth import Requester
from .crypto import decrypt
from .db import PersonalDtbToken, QueryAudit, sessions
from .mcp_context import current_requester
from .mcp_contract import ToolInvalidInput, ToolUnavailable
from .plugins import ExecutorAuthenticationError, ExecutorError
from .query_api import _config, _tables_and_partitions, _validate_sql

MAX_COLUMNS = 30
MAX_LIMIT = 20
DEFAULT_LIMIT = 20


def _partition_sort_key(value):
    text = value.strip()
    try:
        return (2, datetime.fromisoformat(text).timestamp())
    except ValueError:
        pass
    if text.isdigit():
        return (1, int(text))
    return (0, text)


def _qualified(metadata):
    return f"{metadata['database']}.{metadata['name']}"


def _table_metadata(table):
    matches = knowledge.describe(names=[table])
    if not matches:
        raise ToolInvalidInput(f"表 {table} 不在当前知识代")
    if len(matches) > 1:
        raise ToolInvalidInput(f"表名 {table} 不唯一，请使用 database.table")
    metadata = matches[0]
    if not metadata.get("database") or not metadata.get("name"):
        raise ToolUnavailable(f"表 {table} 缺少元数据")
    return metadata


def _field_metadata(metadata):
    fields = metadata.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ToolUnavailable(f"表 {_qualified(metadata)} 缺少字段元数据")
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            raise ToolUnavailable(f"表 {_qualified(metadata)} 的字段元数据无效")
    return fields


def _select_columns(metadata, fields, columns):
    names = [field["name"] for field in fields]
    if columns is None:
        selected = names[:MAX_COLUMNS]
        return selected, len(names) > MAX_COLUMNS
    if not columns:
        raise ToolInvalidInput("columns 不能为空")
    if len(columns) > MAX_COLUMNS:
        raise ToolInvalidInput(f"columns 最多 {MAX_COLUMNS} 列")
    known = {name.lower(): name for name in names}
    selected = []
    for column in columns:
        if not isinstance(column, str) or not column.strip():
            raise ToolInvalidInput("columns 必须是非空字符串")
        actual = known.get(column.strip().lower())
        if actual is None:
            raise ToolInvalidInput(f"字段 {column} 不在表 {_qualified(metadata)} 中")
        if actual in selected:
            raise ToolInvalidInput(f"字段 {actual} 重复")
        selected.append(actual)
    return selected, False


def _latest_partitions(metadata, fields):
    latest = []
    for field in fields:
        if field.get("partition") is not True:
            continue
        values = knowledge.dimension_values(
            metadata["database"], metadata["name"], field["name"]
        )
        candidates = [
            value for value in values if isinstance(value, str) and value.strip()
        ]
        if not candidates:
            raise ToolUnavailable(
                f"无法从知识库确定表 {_qualified(metadata)} 的最新分区: {field['name']}"
            )
        latest.append(
            {"field": field["name"], "value": max(candidates, key=_partition_sort_key)}
        )
    return latest


def _build_sql(metadata, selected, partitions, limit):
    table = exp.Table(
        this=exp.Identifier(this=metadata["name"], quoted=True),
        db=exp.Identifier(this=metadata["database"], quoted=True),
    )
    query = exp.select(
        *[exp.column(exp.Identifier(this=column, quoted=True)) for column in selected]
    ).from_(table)
    for partition in partitions:
        query = query.where(
            exp.EQ(
                this=exp.column(exp.Identifier(this=partition["field"], quoted=True)),
                expression=exp.Literal.string(partition["value"]),
            )
        )
    return query.limit(limit).sql(dialect="doris")


def _start_audit(user, sql):
    audit_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            QueryAudit(
                id=audit_id,
                owner_id=user.id,
                sql=sql,
                status="running",
                row_count=None,
                elapsed_ms=0,
                kind="sample",
            )
        )
    return audit_id


def _finish_audit(audit_id, status, row_count, elapsed_ms):
    with sessions().begin() as db:
        record = db.get(QueryAudit, audit_id)
        if record is not None:
            record.status = status
            record.row_count = row_count
            record.elapsed_ms = elapsed_ms


async def _execute(sql, limit, user: Requester):
    try:
        plugin, config = _config()
    except HTTPException as exc:
        raise ToolUnavailable(str(exc.detail)) from exc
    with sessions()() as db:
        secret = db.get(PersonalDtbToken, user.id)
    if secret is None:
        raise ToolInvalidInput("请先在个人设置中保存 DTB token")
    token = decrypt(secret.encrypted_token)
    executor = plugin.bind(config, token)
    try:
        async with asyncio.timeout(float(config.get("timeout_seconds", 300))):
            key = await executor.submit(sql, limit)
            result = await executor.wait(key)
    except ExecutorAuthenticationError as exc:
        raise ToolInvalidInput("DTB token 无效，请在个人设置中更新") from exc
    except TimeoutError as exc:
        raise ToolUnavailable("DTB 取样超时") from exc
    except ExecutorError as exc:
        raise ToolUnavailable("DTB 取样失败") from exc
    columns, rows = result.get("columns"), result.get("data")
    cap = int(config.get("download_limit", 100000))
    if (
        not isinstance(columns, list)
        or not isinstance(rows, list)
        or len(rows) > cap
        or not all(isinstance(row, list) and len(row) == len(columns) for row in rows)
    ):
        raise ToolUnavailable("执行器返回无效结果或超过下载上限")
    return columns, rows[:limit], len(rows), key


async def sample_table_data(
    table: Annotated[str, Field(min_length=1)],
    columns: Annotated[list[str] | None, Field(default=None)] = None,
    limit: Annotated[int, Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)] = (
        DEFAULT_LIMIT
    ),
) -> dict:
    """Return a few real rows from one table without accepting caller SQL.

    The server selects only the requested columns (all fields by default, at
    most 30), reads the latest partition values from knowledge metadata for
    partitioned tables, and applies a row limit of at most 20. It uses the
    caller's saved DTB token and the configured DTB executor. No confirmation
    is required, but every attempt is audited as a sample.
    """
    user = current_requester()
    if not isinstance(table, str) or not table.strip():
        raise ToolInvalidInput("table 不能为空")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_LIMIT
    ):
        raise ToolInvalidInput(f"limit 必须在 1 到 {MAX_LIMIT} 之间")
    table = table.strip()
    metadata = _table_metadata(table)
    fields = _field_metadata(metadata)
    selected, truncated = _select_columns(metadata, fields, columns)
    partitions = _latest_partitions(metadata, fields)
    sql = _build_sql(metadata, selected, partitions, limit)
    try:
        tree = _validate_sql(sql)
        _tables_and_partitions(tree)
    except HTTPException as exc:
        raise ToolUnavailable(f"取样 SQL 未通过平台安全检查: {exc.detail}") from exc

    started = time.monotonic()
    audit_id = _start_audit(user, sql)
    status, row_count = "failed", None
    try:
        result_columns, rows, _total, _ = await _execute(sql, limit, user)
        status, row_count = "succeeded", len(rows)
        return {
            "table": _qualified(metadata),
            "sql": sql,
            "columns": result_columns,
            "rows": rows,
            "row_count": len(rows),
            "limit": limit,
            "partition": partitions,
            "columns_truncated": truncated,
            "audit_id": audit_id,
        }
    except ToolInvalidInput:
        status = "rejected"
        raise
    except ToolUnavailable:
        status = "failed"
        raise
    finally:
        _finish_audit(
            audit_id, status, row_count, int((time.monotonic() - started) * 1000)
        )
