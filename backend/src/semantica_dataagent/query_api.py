"""Query plans, execution limits and audit for caller-owned queries."""

import asyncio
import json
import time
import uuid
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
from .db import PersonalDtbToken, QueryAudit, QueryExecution, sessions
from .dtb_executor import DtbAuthenticationError, DtbError
from .plugins import configured

router = APIRouter()


class PlanInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100000)


class ExecuteInput(BaseModel):
    limit: int = Field(default=1000, ge=1)


def _validate_sql(sql):
    try:
        expressions = sqlglot.parse(sql, read="doris")
    except sqlglot.errors.SqlglotError as exc:
        raise HTTPException(422, "SQL 无法解析") from exc
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Query):
        raise HTTPException(422, "仅允许 SELECT 或 WITH 查询")
    tree = expressions[0]
    if any(
        isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Into))
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


def _tables_and_partitions(tree):
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
    for scope in traverse_scope(tree):
        where = scope.expression.args.get("where")
        # A predicate beneath OR or NOT cannot prove the partition is constrained.
        terms = []

        def collect(node, terms=terms):
            if isinstance(node, exp.Paren):
                collect(node.this)
            elif isinstance(node, exp.And):
                collect(node.this)
                collect(node.expression)
            elif not isinstance(node, (exp.Or, exp.Not)):
                terms.append(node)

        if where:
            collect(where.this)
        for alias, source in scope.selected_sources.items():
            table = source[1]
            if not isinstance(table, exp.Table):
                continue
            name = ".".join(part for part in (table.db, table.name) if part).lower()
            matches = lookup.get(name, [])
            if len(matches) != 1:
                raise HTTPException(422, f"无法确定表的分区元数据: {name}")
            metadata = matches[0]
            names.append(name)
            partitions = [
                f["name"].lower()
                for f in metadata.get("fields", [])
                if f.get("partition")
            ]
            for field in partitions:

                def valid(term, field=field, alias=alias, scope=scope):
                    if not isinstance(
                        term,
                        (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In),
                    ):
                        return False
                    column = term.this
                    if (
                        not isinstance(column, exp.Column)
                        or column.name.lower() != field
                    ):
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
    return names


@router.post("/api/v1/queries/plan")
def plan(body: PlanInput, user: Annotated[Requester, Depends(current_requester)]):
    tree = _validate_sql(body.sql)
    tables = _tables_and_partitions(tree)
    _, config = _config()
    query_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            QueryExecution(
                id=query_id, owner_id=user.id, sql=body.sql, status="planned"
            )
        )
    return {
        "id": query_id,
        "sql": body.sql,
        "data_source": "doris_ai",
        "tables": tables,
        "estimated_partitions": None,
        "estimated_rows": None,
        "row_limit": config.get("row_limit", 1000),
    }


async def _execute(
    query_id: str,
    body: ExecuteInput,
    user: Annotated[Requester, Depends(current_requester)],
):
    plugin, config = _config()
    if body.limit > config.get("row_limit", 1000):
        raise HTTPException(422, "超过返回行数上限")
    with sessions().begin() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id:
            raise HTTPException(404, "查询不存在")
        secret = db.get(PersonalDtbToken, user.id)
        if secret is None:
            raise HTTPException(400, "请先在个人设置中保存 DTB token")
        token = decrypt(secret.encrypted_token)
        sql = row.sql
        claimed = db.execute(
            update(QueryExecution)
            .where(QueryExecution.id == query_id, QueryExecution.status == "planned")
            .values(status="running")
        ).rowcount
        if not claimed:
            raise HTTPException(409, "查询已执行")
    started = time.monotonic()
    status, result, key = "failed", None, None
    try:
        tree = _validate_sql(sql)
        _tables_and_partitions(tree)
        cap = config.get("download_limit", 100000)
        bounded = f"SELECT * FROM ({tree.sql(dialect='doris')}) AS _dataagent_query LIMIT {cap}"
        executor = plugin.bind(config, token)
        async with asyncio.timeout(config.get("timeout_seconds", 300)):
            key = await executor.submit(bounded, cap)
            result = await executor.wait(key)
        columns, rows = result.get("columns"), result.get("data")
        if (
            not isinstance(columns, list)
            or not isinstance(rows, list)
            or len(rows) > cap
            or not all(isinstance(r, list) and len(r) == len(columns) for r in rows)
        ):
            raise DtbError("DTB 返回无效结果或超过下载上限")
        status = "succeeded"
        return {
            "id": query_id,
            "columns": columns,
            "rows": rows[: body.limit],
            "row_count": len(rows),
        }
    except DtbAuthenticationError as exc:
        raise HTTPException(401, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "查询超时") from exc
    except DtbError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        with sessions().begin() as db:
            row = db.get(QueryExecution, query_id)
            row.status = status
            row.elapsed_ms = int((time.monotonic() - started) * 1000)
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
        sql = row.sql
    started = time.monotonic()
    status, count = "failed", None
    try:
        result = await _execute(query_id, body, user)
        status, count = "succeeded", result["row_count"]
        return result
    except HTTPException as exc:
        status = f"rejected_{exc.status_code}"
        raise
    finally:
        with sessions().begin() as db:
            db.add(
                QueryAudit(
                    owner_id=user.id,
                    sql=sql,
                    status=status,
                    row_count=count,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )


@router.get("/api/v1/query-audit")
def audit(_: Annotated[str, Depends(admin)]):
    with sessions()() as db:
        return [
            {
                "id": row.id,
                "owner_id": row.owner_id,
                "sql": row.sql,
                "status": row.status,
                "row_count": row.row_count,
                "elapsed_ms": row.elapsed_ms,
            }
            for row in db.query(QueryAudit)
            .order_by(QueryAudit.created_at.desc())
            .limit(1000)
        ]


@router.get("/api/v1/queries/{query_id}")
def get_result(query_id: str, user: Annotated[Requester, Depends(current_requester)]):
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


@router.get("/api/v1/queries/{query_id}/download")
async def download(
    query_id: str,
    user: Annotated[Requester, Depends(current_requester)],
    format: Literal["csv", "xlsx", "json"] = "csv",
):
    plugin, config = _config()
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        secret = db.get(PersonalDtbToken, user.id)
        if row is None or row.owner_id != user.id or row.status != "succeeded":
            raise HTTPException(404, "查询结果不存在")
        if secret is None:
            raise HTTPException(400, "请先在个人设置中保存 DTB token")
        key, token = row.query_key, decrypt(secret.encrypted_token)
    try:
        async with asyncio.timeout(config.get("timeout_seconds", 300)):
            content, content_type = await plugin.__class__(
                config["base_url"], token
            ).download(key, format)
    except DtbAuthenticationError as exc:
        raise HTTPException(401, str(exc)) from exc
    except (DtbError, TimeoutError) as exc:
        raise HTTPException(502, "下载失败") from exc
    return Response(
        content,
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="query.{format}"'},
    )
