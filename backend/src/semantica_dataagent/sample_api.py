"""System-generated table samples through the configured query executor."""

import asyncio
import re
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
from .dtb_executor import column_names
from .mcp_context import current_requester
from .mcp_contract import ToolInvalidInput, ToolUnavailable
from .plugins import ExecutorAuthenticationError, ExecutorError
from .query_api import _config, _tables_and_partitions, _validate_sql

MAX_COLUMNS = 30
MAX_LIMIT = 20
DEFAULT_LIMIT = 20
PARTITION_METADATA_LIMIT = 5

_METADATA_TABLES = frozenset(
    {
        "mysql.data_tool_box_emr.metadata_table",
        "mysql.data_tool_box_emr.metadata_partition",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


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


def _table_metadata(table, project_id=None):
    if project_id in (None, "default"):
        matches = knowledge.describe(names=[table])
    else:
        matches = knowledge.describe(names=[table], project_id=project_id)
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


def _metadata_identifier(value, label):
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ToolInvalidInput(f"表元数据中的{label}不合法: {value!r}")
    return value


def _metadata_partition_sql(database, table):
    """Build the fixed, read-only partition metadata query for one table."""
    database = _metadata_identifier(database, "库名")
    table = _metadata_identifier(table, "表名")
    database_literal = exp.Literal.string(database).sql(dialect="doris")
    table_literal = exp.Literal.string(table).sql(dialect="doris")
    return (
        "SELECT partition_name FROM ("
        "SELECT partition_name, ROW_NUMBER() OVER ("
        "PARTITION BY database_name, table_name "
        "ORDER BY split_part(partition_name, '=', 2) DESC) AS rk "
        "FROM mysql.data_tool_box_emr.metadata_table AS t "
        "LEFT JOIN mysql.data_tool_box_emr.metadata_partition AS p "
        "ON t.table_id = p.table_id "
        f"WHERE t.database_name = {database_literal} "
        f"AND t.table_name = {table_literal} "
        "AND t.catalog_type = 'HIVE' AND t.status = '在用'"
        ") ranked WHERE rk = 1"
    )


def _validate_metadata_sql(sql, database, table):
    """Validate the generated metadata query independently from business tables."""
    try:
        tree = _validate_sql(sql)
    except HTTPException as exc:
        raise ToolUnavailable(f"分区元数据查询未通过安全检查: {exc.detail}") from exc
    tables = {
        ".".join(part for part in (node.catalog, node.db, node.name) if part).lower()
        for node in tree.find_all(exp.Table)
    }
    if tables != _METADATA_TABLES:
        raise ToolUnavailable("分区元数据查询只能访问两张元数据表")
    for node in tree.walk():
        if isinstance(node, (exp.Or, exp.Not)):
            raise ToolUnavailable("分区元数据查询只能使用等值过滤")
        if isinstance(node, exp.Predicate) and not isinstance(node, exp.EQ):
            raise ToolUnavailable("分区元数据查询只能使用等值过滤")
    filters = {}
    for node in tree.find_all(exp.EQ):
        left, right = node.this, node.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            filters.setdefault(left.name.lower(), []).append(str(right.this))
        elif (
            isinstance(left, exp.Column)
            and isinstance(right, exp.Column)
            and left.name.lower() == right.name.lower() == "table_id"
        ):
            continue
        else:
            raise ToolUnavailable("分区元数据查询只能按库名和表名过滤")
    if filters.get("database_name") != [database]:
        raise ToolUnavailable("分区元数据查询缺少库名过滤")
    if filters.get("table_name") != [table]:
        raise ToolUnavailable("分区元数据查询缺少表名过滤")
    for name, values in filters.items():
        if name in {"database_name", "table_name"}:
            continue
        if name == "catalog_type" and values == ["HIVE"]:
            continue
        if name == "status" and values == ["在用"]:
            continue
        if name == "rk" and values == ["1"]:
            continue
        raise ToolUnavailable("分区元数据查询只能按库名和表名过滤")
    return tree


def _parse_partition_name(value):
    if not isinstance(value, str) or not value.strip():
        return None
    parts = {}
    for segment in value.split("/"):
        if "=" not in segment:
            return None
        name, _, raw = segment.partition("=")
        name, raw = name.strip(), raw.strip()
        if not name or not raw:
            return None
        parts[name.lower()] = raw
    return parts


def _partitions_from_metadata(fields, columns, rows):
    names = column_names(columns)
    if (
        names is None
        or not names
        or not isinstance(rows, list)
        or not all(isinstance(row, list) and len(row) == len(names) for row in rows)
    ):
        return None, "分区元数据返回格式无效"
    lowered = [name.lower() for name in names]
    if "partition_name" not in lowered:
        return None, "分区元数据缺少 partition_name"
    index = lowered.index("partition_name")
    partition_fields = [
        field["name"] for field in fields if field.get("partition") is True
    ]
    if not rows:
        return None, "分区元数据为空"
    candidates = []
    for row in rows:
        parts = _parse_partition_name(row[index])
        if parts is None:
            continue
        if any(name.lower() not in parts for name in partition_fields):
            continue
        key = tuple(
            _partition_sort_key(parts[name.lower()]) for name in partition_fields
        )
        candidates.append(
            (
                key,
                [
                    {"field": name, "value": parts[name.lower()]}
                    for name in partition_fields
                ],
            )
        )
    if not candidates:
        return None, "分区元数据中的 partition_name 格式无效或与分区字段不匹配"
    return max(candidates, key=lambda item: item[0])[1], None


def _knowledge_partitions(metadata, fields, project_id=None):
    latest = []
    for field in fields:
        if field.get("partition") is not True:
            continue
        arguments = (metadata["database"], metadata["name"], field["name"])
        if project_id in (None, "default"):
            values = knowledge.dimension_values(*arguments)
        else:
            values = knowledge.dimension_values(*arguments, project_id=project_id)
        candidates = [
            value for value in values if isinstance(value, str) and value.strip()
        ]
        if not candidates:
            return None, f"知识库缺少分区值: {field['name']}"
        latest.append(
            {"field": field["name"], "value": max(candidates, key=_partition_sort_key)}
        )
    return latest, None


async def _latest_partitions(metadata, fields, runtime, project_id=None):
    if not any(field.get("partition") is True for field in fields):
        return [], None
    reason = None
    if runtime is not None:
        plugin, config, token = runtime
        sql = _metadata_partition_sql(metadata["database"], metadata["name"])
        _validate_metadata_sql(sql, metadata["database"], metadata["name"])
        try:
            columns, rows, _total, _key = await _run_sql(
                plugin,
                config,
                token,
                sql,
                PARTITION_METADATA_LIMIT,
                timeout_message="读取 DTB 分区元数据超时",
                failure_message="读取 DTB 分区元数据失败",
            )
        except ToolInvalidInput:
            raise
        except ToolUnavailable as exc:
            reason = str(exc)
        else:
            partitions, malformed = _partitions_from_metadata(fields, columns, rows)
            if partitions is not None:
                return partitions, None
            reason = malformed
    fallback, knowledge_reason = _knowledge_partitions(
        metadata, fields, project_id=project_id
    )
    if fallback is not None:
        return fallback, None
    details = reason or knowledge_reason
    if reason and knowledge_reason:
        details = f"{reason}；知识库：{knowledge_reason}"
    return None, (
        f"无法从 DTB 分区元数据或知识库确定表 {_qualified(metadata)} 的最新分区: {details}"
    )


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


def _runtime(user: Requester):
    try:
        plugin, config = _config()
    except HTTPException as exc:
        raise ToolUnavailable(str(exc.detail)) from exc
    with sessions()() as db:
        secret = db.get(PersonalDtbToken, user.id)
    if secret is None:
        raise ToolInvalidInput("请先在个人设置中保存 DTB token")
    return plugin, config, decrypt(secret.encrypted_token)


async def _run_sql(
    plugin,
    config,
    token,
    sql,
    limit,
    *,
    timeout_message,
    failure_message,
):
    executor = plugin.bind(config, token)
    try:
        async with asyncio.timeout(float(config.get("timeout_seconds", 300))):
            key = await executor.submit(sql, limit)
            result = await executor.wait(key)
    except ExecutorAuthenticationError as exc:
        raise ToolInvalidInput("DTB token 无效，请在个人设置中更新") from exc
    except TimeoutError as exc:
        raise ToolUnavailable(timeout_message) from exc
    except ExecutorError as exc:
        raise ToolUnavailable(failure_message) from exc
    columns, rows = result.get("columns"), result.get("data")
    names = column_names(columns)
    cap = int(config.get("download_limit", 100000))
    if names is None:
        raise ToolUnavailable("执行器返回结果格式无效")
    if (
        not isinstance(rows, list)
        or len(rows) > cap
        or not all(isinstance(row, list) and len(row) == len(names) for row in rows)
    ):
        raise ToolUnavailable("执行器返回无效结果或超过下载上限")
    return names, rows[:limit], len(rows), key


async def sample_table_data(
    table: Annotated[str, Field(min_length=1)],
    columns: Annotated[list[str] | None, Field(default=None)] = None,
    limit: Annotated[int, Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)] = (
        DEFAULT_LIMIT
    ),
    project_id: str | None = None,
) -> dict:
    """Return a few real rows from one table without accepting caller SQL.

    The server selects only the requested columns (all fields by default, at
    most 30), asks the DTB partition metadata for the latest partition of
    partitioned tables (falling back to knowledge metadata), and applies a row
    limit of at most 20. It uses the caller's saved DTB token and the configured
    DTB executor. No confirmation is required, but every attempt that reaches
    execution is audited as a sample.
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
    metadata = _table_metadata(table, project_id=project_id)
    fields = _field_metadata(metadata)
    selected, truncated = _select_columns(metadata, fields, columns)
    runtime = None
    runtime_error = None
    try:
        runtime = _runtime(user)
    except (ToolInvalidInput, ToolUnavailable) as exc:
        runtime_error = exc
    partitions, partition_error = await _latest_partitions(
        metadata, fields, runtime, project_id=project_id
    )
    if partitions is None:
        if isinstance(runtime_error, ToolInvalidInput):
            raise runtime_error
        raise ToolUnavailable(partition_error)
    sql = _build_sql(metadata, selected, partitions, limit)
    try:
        tree = _validate_sql(sql)
        _tables_and_partitions(tree, project_id=project_id)
    except HTTPException as exc:
        raise ToolUnavailable(f"取样 SQL 未通过平台安全检查: {exc.detail}") from exc

    started = time.monotonic()
    audit_id = _start_audit(user, sql)
    status, row_count = "failed", None
    try:
        if runtime is None:
            raise runtime_error
        plugin, config, token = runtime
        result_columns, rows, _total, _ = await _run_sql(
            plugin,
            config,
            token,
            sql,
            limit,
            timeout_message="DTB 取样超时",
            failure_message="DTB 取样失败",
        )
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
