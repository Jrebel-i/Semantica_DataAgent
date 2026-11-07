"""Query plans, execution limits and audit for caller-owned queries."""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

import sqlglot
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from . import knowledge
from .auth import Requester, admin, current_requester
from .crypto import decrypt
from .db import (
    Conversation,
    PersonalDtbToken,
    QueryAudit,
    QueryExecution,
    Turn,
    sessions,
)
from .dtb_executor import column_names
from .plugins import ExecutorAuthenticationError, ExecutorError, configured

router = APIRouter()


class PlanInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100000)


class ExecuteInput(BaseModel):
    limit: int | None = Field(default=None, ge=1)


def _validate_sql(sql):
    try:
        expressions = sqlglot.parse(sql, read="doris")
    except sqlglot.errors.SqlglotError as exc:
        raise HTTPException(422, "SQL 无法解析") from exc
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Query):
        raise HTTPException(422, "仅允许 SELECT 或 WITH 查询")
    tree = expressions[0]
    if any(
        isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock))
        for node in tree.walk()
    ):
        raise HTTPException(422, "仅允许 SELECT 或 WITH 查询")
    return tree


def _config():
    for category, plugin, enabled, params in configured():
        if category == "executor" and enabled:
            url = params.get("base_url", "")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise HTTPException(503, "执行器地址无效")
            return plugin, params
    raise HTTPException(503, "尚未启用执行器")


def _partition_terms(where):
    terms = []

    def collect(node):
        if isinstance(node, exp.Paren):
            collect(node.this)
        elif isinstance(node, exp.And):
            collect(node.this)
            collect(node.expression)
        elif not isinstance(node, (exp.Or, exp.Not)):
            terms.append(node)

    if where:
        collect(where.this)
    return terms


def _latest_partition_subquery(term, field, alias, scope, table):
    if not isinstance(term, exp.EQ):
        return None
    column = term.this
    if not isinstance(column, exp.Column) or column.name.lower() != field:
        return None
    if column.table and column.table.lower() != alias.lower():
        return None
    if not column.table and len(scope.selected_sources) != 1:
        return None
    subquery = term.expression
    if not isinstance(subquery, exp.Subquery):
        return None
    select = subquery.this
    if not isinstance(select, exp.Select):
        return None
    if any(
        select.args.get(key)
        for key in (
            "with_",
            "distinct",
            "where",
            "group",
            "having",
            "qualify",
            "windows",
            "order",
            "limit",
            "offset",
            "fetch",
            "joins",
            "connect",
            "match",
            "sample",
            "pivot",
            "unpivot",
            "lateral",
            "prewhere",
            "cluster",
            "distribute",
            "sort",
        )
    ):
        return None
    if len(select.expressions) != 1:
        return None
    projection = select.expressions[0]
    if (
        not isinstance(projection, exp.Max)
        or projection.args.get("expressions")
        or projection.args.get("over")
        or not isinstance(projection.this, exp.Column)
        or projection.this.name.lower() != field
    ):
        return None
    from_ = select.args.get("from_")
    if not isinstance(from_, exp.From) or not isinstance(from_.this, exp.Table):
        return None
    inner_table = from_.this
    if (
        not isinstance(inner_table.this, exp.Identifier)
        or inner_table.catalog
        or inner_table.name.lower() != table.name.lower()
        or (inner_table.db or "").lower() != (table.db or "").lower()
    ):
        return None
    max_column = projection.this
    if max_column.table and max_column.table.lower() not in {
        inner_table.alias_or_name.lower(),
        inner_table.name.lower(),
    }:
        return None
    return select


def _partition_analysis(tree):
    try:
        tables = knowledge.describe()
    except Exception as exc:
        raise HTTPException(503, "无法读取分区元数据") from exc
    lookup = {}
    for table in tables:
        lookup.setdefault(table["name"].lower(), []).append(table)
        lookup.setdefault((table["database"] + "." + table["name"]).lower(), []).append(
            table
        )
    names = []
    records = []
    for scope in traverse_scope(tree):
        # A predicate beneath OR or NOT cannot prove the partition is constrained.
        terms = _partition_terms(scope.expression.args.get("where"))
        for alias, source in scope.selected_sources.items():
            table = source[1]
            if not isinstance(table, exp.Table):
                continue
            if not isinstance(table.this, exp.Identifier) or table.catalog:
                raise HTTPException(422, "不支持此表来源")
            name = ".".join(part for part in (table.db, table.name) if part).lower()
            matches = lookup.get(name, [])
            if len(matches) != 1:
                raise HTTPException(422, f"无法确定表的分区元数据: {name}")
            metadata = matches[0]
            fields = metadata.get("fields")
            if (
                not isinstance(fields, list)
                or not fields
                or any(
                    not isinstance(f, dict)
                    or not isinstance(f.get("name"), str)
                    or not isinstance(f.get("partition"), bool)
                    for f in fields
                )
            ):
                raise HTTPException(503, f"无法确定表的分区元数据: {name}")
            names.append(name)
            partitions = [f["name"].lower() for f in fields if f.get("partition")]
            records.append((scope, terms, alias, table, name, partitions))

    latest_runtime = False
    allowed = set()
    for scope, terms, alias, table, _, partitions in records:
        for field in partitions:
            for term in terms:
                inner = _latest_partition_subquery(term, field, alias, scope, table)
                if inner is not None:
                    allowed.add(id(inner))
                    latest_runtime = True

    for scope, terms, alias, table, name, partitions in records:
        if id(scope.expression) in allowed:
            continue
        for field in partitions:

            def valid(term, field=field, alias=alias, scope=scope, table=table):
                if _latest_partition_subquery(term, field, alias, scope, table):
                    return True
                if not isinstance(
                    term,
                    (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In),
                ):
                    return False
                column = term.this
                if not isinstance(column, exp.Column) or column.name.lower() != field:
                    return False
                if column.table and column.table.lower() != alias.lower():
                    return False
                if not column.table and len(scope.selected_sources) != 1:
                    return False
                values = [
                    value
                    for key, value in term.args.items()
                    if key != "this" and value is not None
                ]
                flat = [
                    item
                    for value in values
                    for item in (value if isinstance(value, list) else [value])
                ]
                return bool(flat) and all(
                    isinstance(value, exp.Literal) for value in flat
                )

            if not any(valid(term) for term in terms):
                raise HTTPException(422, f"分区表 {name} 缺少分区条件: {field}")
    return names, latest_runtime


def _tables_and_partitions(tree):
    names, _ = _partition_analysis(tree)
    return names


def _cleanup_expired_conversation_results(query_id: str, user: Requester) -> None:
    """Retention ends access to conversation results, not just listing them."""
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        conversation_id = (
            row.conversation_id if row is not None and row.owner_id == user.id else None
        )
    if conversation_id:
        from .conversation_api import cleanup_expired_sessions

        cleanup_expired_sessions()
    else:
        from .conversation_api import retention_cutoff

        with sessions().begin() as db:
            row = db.get(QueryExecution, query_id)
            cutoff = retention_cutoff(db)
            if (
                row is not None
                and row.owner_id == user.id
                and row.plan_data
                and json.loads(row.plan_data).get("source") == "mcp"
                and row.created_at.replace(tzinfo=UTC) < cutoff
            ):
                db.delete(row)


@router.post("/api/v1/queries/plan")
async def plan(body: PlanInput, user: Annotated[Requester, Depends(current_requester)]):
    return await _create_plan(body, user)


async def _create_plan(body, user, conversation_id=None, turn_id=None, source=None):
    tree = _validate_sql(body.sql)
    tables, latest_partition = _partition_analysis(tree)
    plugin, config = _config()
    estimate = await plugin.bind(config, "").estimate(body.sql)
    query_id = uuid.uuid4().hex
    plan_data = {
        "id": query_id,
        "sql": body.sql,
        "data_source": "doris_ai",
        "tables": tables,
        "estimated_partitions": estimate.get("partitions"),
        "estimated_rows": estimate.get("rows"),
        "row_limit": config.get("row_limit", 1000),
        "execution_limit": config.get("download_limit", 100000),
    }
    if latest_partition:
        plan_data["partition_selection"] = "latest_at_runtime"
    if source:
        plan_data["source"] = source
    with sessions().begin() as db:
        if conversation_id is not None:
            conversation = db.get(Conversation, conversation_id)
            turn = db.get(Turn, turn_id) if turn_id else None
            if (
                conversation is None
                or conversation.owner_id != user.id
                or turn is None
                or turn.conversation_id != conversation_id
                or turn.status != "running"
            ):
                raise ValueError("Active conversation turn not found")
        db.add(
            QueryExecution(
                id=query_id,
                owner_id=user.id,
                sql=body.sql,
                status="planned",
                conversation_id=conversation_id,
                turn_id=turn_id,
                plan_data=json.dumps(plan_data, ensure_ascii=False),
            )
        )
    return plan_data


async def propose_query(sql: str) -> dict:
    """Propose a validated Doris SQL query for the questioner to confirm.

    This creates a plan only; it does not execute the query. After proposing,
    stop and wait for the questioner to confirm execution.
    """
    from . import sql_validation
    from .mcp_context import current_conversation_id, current_requester, current_turn_id

    if sql_validation.validate_sql(sql, dialect="doris").get("status") != "pass":
        raise ValueError("SQL validation did not pass")
    conversation_id = current_conversation_id()
    supplied_turn = current_turn_id()
    if conversation_id and (
        not supplied_turn
        or not supplied_turn.isascii()
        or not supplied_turn.isdecimal()
    ):
        raise ValueError("Active conversation turn not found")
    return await _create_plan(
        PlanInput(sql=sql),
        current_requester(),
        conversation_id,
        int(supplied_turn) if supplied_turn else None,
    )


async def execute_query(
    sql: str | None = None, confirmation_id: str | None = None
) -> dict:
    """Plan SQL, then execute only after the user explicitly confirms the plan.

    First supply only sql. Show the returned plan to the user and wait for their
    confirmation. Then supply only confirmation_id. Signed download links are
    short-lived bearer credentials. Never confirm on behalf of the user.
    """
    from .conversation_api import public_base_url
    from .mcp_context import current_conversation_id, current_requester
    from .mcp_contract import ToolInvalidInput

    if bool(sql) == bool(confirmation_id):
        raise ToolInvalidInput("请只提供 SQL 或确认 id")
    if current_conversation_id():
        raise ToolInvalidInput("在线查询须在会话中确认")
    user = current_requester()
    try:
        if sql:
            return await _create_plan(PlanInput(sql=sql), user, source="mcp")
        _cleanup_expired_conversation_results(confirmation_id, user)
        with sessions()() as db:
            row = db.get(QueryExecution, confirmation_id)
            if (
                row is None
                or row.owner_id != user.id
                or row.conversation_id is not None
            ):
                raise HTTPException(404, "查询不存在")
        result = await _execute_with_audit(
            confirmation_id, ExecuteInput(), user, kind="mcp"
        )
        result["download_urls"] = _download_links(
            confirmation_id, user, public_base_url()
        )
        return result
    except HTTPException as exc:
        raise ToolInvalidInput(str(exc.detail)) from exc


async def _execute(
    query_id: str,
    body: ExecuteInput,
    user: Requester,
    audit_id: str,
):
    plugin, config = _config()
    limit = body.limit if body.limit is not None else config.get("row_limit", 1000)
    if limit > config.get("row_limit", 1000):
        raise HTTPException(422, "超过返回行数上限")
    with sessions().begin() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id:
            raise HTTPException(404, "查询不存在")
        cutoff = datetime.now(UTC) - timedelta(
            seconds=config.get("confirmation_ttl_seconds", 1800)
        )
        if row.created_at.replace(tzinfo=UTC) <= cutoff:
            raise HTTPException(410, "查询确认已过期")
        secret = db.get(PersonalDtbToken, user.id)
        if secret is None:
            raise HTTPException(400, "请先在个人设置中保存 DTB token")
        token = decrypt(secret.encrypted_token)
        sql = row.sql
        saved_plan = json.loads(row.plan_data) if row.plan_data else {}
        claimed = db.execute(
            update(QueryExecution)
            .where(
                QueryExecution.id == query_id,
                QueryExecution.status == "planned",
                QueryExecution.created_at > cutoff,
            )
            .values(status="running")
            .execution_options(synchronize_session=False)
        ).rowcount
        if not claimed:
            raise HTTPException(409, "查询已执行")
    started = time.monotonic()
    status, result, key = "failed", None, None
    try:
        tree = _validate_sql(sql)
        _tables_and_partitions(tree)
        cap = min(
            config.get("download_limit", 100000),
            saved_plan.get("execution_limit", config.get("download_limit", 100000)),
        )
        bounded = f"SELECT * FROM ({tree.sql(dialect='doris')}) AS _dataagent_query LIMIT {cap}"
        executor = plugin.bind(config, token)
        async with asyncio.timeout(config.get("timeout_seconds", 300)):
            key = await executor.submit(bounded, cap)
            result = await executor.wait(key)
        columns, rows = result.get("columns"), result.get("data")
        names = column_names(columns)
        if names is None:
            raise ExecutorError("执行器返回结果格式无效")
        if (
            not isinstance(rows, list)
            or len(rows) > cap
            or not all(isinstance(r, list) and len(r) == len(names) for r in rows)
        ):
            raise ExecutorError("执行器返回无效结果或超过下载上限")
        status = "succeeded"
        return {
            "id": query_id,
            "columns": names,
            "rows": rows[:limit],
            "row_count": len(rows),
            "possibly_truncated": len(rows) >= min(limit, cap),
        }
    except ExecutorAuthenticationError as exc:
        raise HTTPException(401, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "查询超时") from exc
    except ExecutorError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        with sessions().begin() as db:
            row = db.get(QueryExecution, query_id)
            row.status = status
            row.elapsed_ms = int((time.monotonic() - started) * 1000)
            record = db.get(QueryAudit, audit_id)
            record.status = status
            record.elapsed_ms = row.elapsed_ms
            record.row_count = len(result["data"]) if status == "succeeded" else None
            if status == "succeeded":
                row.rows = json.dumps(result, ensure_ascii=False)
                row.row_count = len(result["data"])
                row.query_key = key


@router.post("/api/v1/queries/{query_id}/execute")
async def execute(
    query_id: str,
    body: ExecuteInput,
    user: Annotated[Requester, Depends(current_requester)],
):
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id:
            raise HTTPException(404, "查询不存在")
        if row.conversation_id is not None:
            raise HTTPException(403, "在线查询需要在会话中确认")
    return await _execute_with_audit(query_id, body, user)


async def execute_for_turn(
    query_id: str, user: Requester, conversation_id: str, turn_id: int
):
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if (
            row is None
            or row.owner_id != user.id
            or row.conversation_id != conversation_id
            or row.turn_id != turn_id
        ):
            raise HTTPException(404, "查询不存在")
    return await _execute_with_audit(query_id, ExecuteInput(), user)


async def _execute_with_audit(
    query_id: str, body: ExecuteInput, user: Requester, kind="query"
):
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id:
            raise HTTPException(404, "查询不存在")
        sql = row.sql
    started = time.monotonic()
    audit_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            QueryAudit(
                id=audit_id,
                owner_id=user.id,
                sql=sql,
                kind=kind,
                status="running",
                row_count=None,
                elapsed_ms=0,
            )
        )
    status, count = "failed", None
    try:
        result = await _execute(query_id, body, user, audit_id)
        status, count = "succeeded", result["row_count"]
        return result
    except HTTPException as exc:
        status = f"rejected_{exc.status_code}"
        raise
    finally:
        with sessions().begin() as db:
            record = db.get(QueryAudit, audit_id)
            record.status = status
            record.row_count = count
            record.elapsed_ms = int((time.monotonic() - started) * 1000)


@router.get("/api/v1/query-audit")
def audit(_: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        return [
            {
                "id": row.id,
                "owner_id": row.owner_id,
                "sql": row.sql,
                "status": row.status,
                "kind": row.kind,
                "type": "取样" if row.kind == "sample" else "查询",
                "row_count": row.row_count,
                "elapsed_ms": row.elapsed_ms,
            }
            for row in db.query(QueryAudit)
            .order_by(QueryAudit.created_at.desc())
            .limit(1000)
        ]


@router.get("/api/v1/queries/{query_id}")
def get_result(query_id: str, user: Annotated[Requester, Depends(current_requester)]):
    _cleanup_expired_conversation_results(query_id, user)
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id:
            raise HTTPException(404, "查询不存在")
        result = json.loads(row.rows) if row.rows else None
        _, config = _config()
        return {
            "id": row.id,
            "status": row.status,
            "row_count": row.row_count,
            "elapsed_ms": row.elapsed_ms,
            "columns": result.get("columns") if result else None,
            "rows": result["data"][: config.get("row_limit", 1000)] if result else None,
        }


DOWNLOAD_LINK_TTL_SECONDS = 30 * 60
DOWNLOAD_FORMATS = ("csv", "xlsx", "json")


def _download_signature(query_id: str, format: str, expires: int) -> str:
    message = f"{query_id}:{format}:{expires}".encode()
    return hmac.new(
        os.environ["DATAAGENT_ENCRYPTION_KEY"].encode(), message, hashlib.sha256
    ).hexdigest()


def _audit_download(owner_id: str, query_id: str, format: str, kind: str) -> None:
    with sessions().begin() as db:
        db.add(
            QueryAudit(
                id=uuid.uuid4().hex,
                owner_id=owner_id,
                sql=json.dumps({"query_id": query_id, "format": format}),
                kind=kind,
                status="succeeded",
                row_count=None,
                elapsed_ms=0,
            )
        )


def _download_links(query_id: str, user: Requester, base: str) -> dict[str, str]:
    _cleanup_expired_conversation_results(query_id, user)
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id or row.status != "succeeded":
            raise HTTPException(404, "查询结果不存在")
    expires = int(time.time()) + DOWNLOAD_LINK_TTL_SECONDS
    links = {}
    for fmt in DOWNLOAD_FORMATS:
        signature = _download_signature(query_id, fmt, expires)
        links[fmt] = (
            f"{base}/api/v1/queries/{query_id}/download-link/{fmt}/{expires}/{signature}"
        )
        _audit_download(user.id, query_id, fmt, "download_link_created")
    return links


@router.post("/api/v1/queries/{query_id}/download-links")
def create_download_links(
    query_id: str, user: Annotated[Requester, Depends(current_requester)]
):
    from .conversation_api import public_base_url

    return _download_links(query_id, user, public_base_url())


@router.get("/api/v1/queries/{query_id}/download-link/{format}/{expires}/{signature}")
async def download_link(query_id: str, format: str, expires: str, signature: str):
    if not expires.isascii() or not expires.isdigit() or len(expires) > 12:
        raise HTTPException(404, "查询结果不存在")
    deadline = int(expires)
    if (
        format not in DOWNLOAD_FORMATS
        or deadline <= int(time.time())
        or not signature.isascii()
        or not hmac.compare_digest(
            _download_signature(query_id, format, deadline), signature
        )
    ):
        raise HTTPException(404, "查询结果不存在")
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None:
            raise HTTPException(404, "查询结果不存在")
        user = Requester(id=row.owner_id, role="user", token="")
    response = await download(query_id, user, format)
    _audit_download(user.id, query_id, format, "download_link_used")
    return response


@router.get("/api/v1/queries/{query_id}/download")
async def download(
    query_id: str,
    user: Annotated[Requester, Depends(current_requester)],
    format: Literal["csv", "xlsx", "json"] = "csv",
):
    _cleanup_expired_conversation_results(query_id, user)
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id or row.status != "succeeded":
            raise HTTPException(404, "查询结果不存在")
        if row.rows and (
            row.conversation_id is not None
            or (row.plan_data and json.loads(row.plan_data).get("source") == "mcp")
        ):
            saved = json.loads(row.rows)
            names = column_names(saved["columns"])
            values = saved["data"]
            if format == "json":
                content = json.dumps(
                    {"columns": names, "rows": values}, ensure_ascii=False
                ).encode()
                content_type = "application/json"
            elif format == "csv":
                buffer = io.StringIO(newline="")
                writer = csv.writer(buffer, lineterminator="\n")
                writer.writerow(names)
                writer.writerows(values)
                content, content_type = buffer.getvalue().encode("utf-8"), "text/csv"
            else:
                from openpyxl import Workbook
                from openpyxl.cell import WriteOnlyCell

                workbook = Workbook(write_only=True)
                sheet = workbook.create_sheet()
                for values_row in [names, *values]:
                    cells = []
                    for value in values_row:
                        if isinstance(value, (dict, list)):
                            value = json.dumps(value, ensure_ascii=False)
                        cell = WriteOnlyCell(sheet, value=value)
                        if isinstance(value, str):
                            cell.data_type = "s"
                        cells.append(cell)
                    sheet.append(cells)
                buffer = io.BytesIO()
                workbook.save(buffer)
                content = buffer.getvalue()
                content_type = (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            return Response(
                content,
                media_type=content_type,
                headers={
                    "Content-Disposition": f'attachment; filename="query.{format}"'
                },
            )
        secret = db.get(PersonalDtbToken, user.id)
        if secret is None:
            raise HTTPException(400, "请先在个人设置中保存 DTB token")
        key, token = row.query_key, decrypt(secret.encrypted_token)
    plugin, config = _config()
    try:
        async with asyncio.timeout(config.get("timeout_seconds", 300)):
            content, content_type = await plugin.bind(config, token).download(
                key, format
            )
    except ExecutorAuthenticationError as exc:
        raise HTTPException(401, str(exc)) from exc
    except (ExecutorError, TimeoutError) as exc:
        raise HTTPException(502, "下载失败") from exc
    return Response(
        content,
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="query.{format}"'},
    )
