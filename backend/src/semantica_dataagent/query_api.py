"""Query plans, execution limits and audit for caller-owned queries."""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

import sqlglot
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from . import knowledge
from .auth import Requester, _authenticated, admin, bearer, current_requester
from .crypto import decrypt
from .db import (
    AgentCredential,
    Conversation,
    PersonalDtbToken,
    QueryAudit,
    QueryExecution,
    Token,
    Turn,
    digest,
    sessions,
)
from .dtb_executor import column_names
from .failure_diagnostics import safe_dtb_category, safe_query_stage
from .plugins import ExecutorAuthenticationError, ExecutorError, configured
from .project_api import require_query_project_access
from .project_context import project_is_frozen
from .table_catalog import (
    executable_name,
    match_table,
    same_physical_table,
    table_lookup,
)

router = APIRouter()
logger = logging.getLogger(__name__)


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


def _query_failure(stage, category):
    return {
        "stage": safe_query_stage(stage),
        "category": safe_dtb_category(category),
    }


def _annotate_query_failure(exc, stage, category):
    failure = _query_failure(stage, category)
    exc.failure_stage = failure["stage"]
    exc.failure_category = failure["category"]
    return exc


def _record_execution_failure(db, row, stage, category):
    """Persist only the fixed diagnostic fields on the existing plan JSON."""
    if row is None or not row.plan_data:
        return
    try:
        plan = json.loads(row.plan_data)
    except (TypeError, ValueError):
        return
    if not isinstance(plan, dict):
        return
    plan["execution_failure"] = _query_failure(stage, category)
    row.plan_data = json.dumps(plan, ensure_ascii=False)


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


def _latest_partition_subquery(term, field, alias, scope, table, metadata, lookup):
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
    if not same_physical_table(inner_table, table, metadata, lookup):
        return None
    max_column = projection.this
    if max_column.table and max_column.table.lower() not in {
        inner_table.alias_or_name.lower(),
        inner_table.name.lower(),
    }:
        return None
    return select


def _partition_issues(tree, tables):
    """Return table names, latest-partition usage and actionable problems.

    ``tables`` uses the same shape as ``knowledge.describe()``.  Keeping the
    rules in one place lets ``validate_sql`` show the same rejection that the
    execution path would raise, instead of maintaining a second copy.
    """
    lookup = table_lookup(tables)
    names = []
    records = []
    problems = []
    for scope in traverse_scope(tree):
        # A predicate beneath OR or NOT cannot prove the partition is constrained.
        terms = _partition_terms(scope.expression.args.get("where"))
        for alias, source in scope.selected_sources.items():
            table = source[1]
            if not isinstance(table, exp.Table):
                continue
            if not isinstance(table.this, exp.Identifier):
                problems.append(
                    (
                        422,
                        {
                            "kind": "partition_metadata",
                            "table": "<unsupported>",
                            "condition": "<unsupported>",
                            "message": "不支持此表来源",
                            "suggestion": (
                                "只使用 catalog.database.table 或 database.table "
                                "形式的物理表。"
                            ),
                        },
                    )
                )
                continue
            metadata, reason = match_table(table, lookup)
            if metadata is None:
                if reason == "ambiguous":
                    message = "无法确定表在哪个目录下"
                    suggestion = (
                        "存在多个目录下的同名表，请使用 catalog.database.table。"
                    )
                elif reason == "unqualified":
                    message = "SQL 省略了表的目录，无法确认与知识库中的表一致"
                    suggestion = (
                        "请使用 catalog.database.table 完整限定表名"
                        "（目录以 describe_tables 返回的 catalog 为准）；"
                        "省略目录可能落到默认目录。"
                    )
                elif reason == "catalog":
                    message = "无法确认表的目录元数据"
                    suggestion = (
                        "知识库没有该目录的元数据；请使用 database.table，"
                        "不要猜测目录。"
                    )
                else:
                    message = "无法确定表的分区元数据"
                    suggestion = "确认知识库中存在唯一的 database.table 元数据。"
                problems.append(
                    (
                        422,
                        {
                            "kind": "partition_metadata",
                            "table": "<unknown>",
                            "message": message,
                            "suggestion": suggestion,
                        },
                    )
                )
                continue
            name = executable_name(metadata, table)
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
                problems.append(
                    (
                        503,
                        {
                            "kind": "partition_metadata",
                            "table": name,
                            "message": f"无法确定表的分区元数据: {name}",
                            "suggestion": "重新构建知识库，确保字段带有 partition 布尔标记。",
                        },
                    )
                )
                continue
            if name not in names:
                names.append(name)
            partitions = [f["name"].lower() for f in fields if f.get("partition")]
            records.append((scope, terms, alias, table, name, partitions, metadata))

    latest_runtime = False
    allowed = set()
    for scope, terms, alias, table, _, partitions, metadata in records:
        for field in partitions:
            for term in terms:
                inner = _latest_partition_subquery(
                    term, field, alias, scope, table, metadata, lookup
                )
                if inner is not None:
                    allowed.add(id(inner))
                    latest_runtime = True

    for scope, terms, alias, table, name, partitions, metadata in records:
        if id(scope.expression) in allowed:
            continue
        for field in partitions:

            def valid(
                term,
                field=field,
                alias=alias,
                scope=scope,
                table=table,
                metadata=metadata,
            ):
                if _latest_partition_subquery(
                    term, field, alias, scope, table, metadata, lookup
                ):
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
                qualified = ".".join(part for part in (table.db, table.name) if part)
                problems.append(
                    (
                        422,
                        {
                            "kind": "partition",
                            "table": name,
                            "field": field,
                            "message": f"分区表 {name} 缺少分区条件: {field}",
                            "suggestion": (
                                f"在 WHERE 中限定 {field}：快照表用 "
                                f"{field} = (SELECT MAX({field}) FROM {qualified})；"
                                f"增量表用 {field} 的范围条件，如 "
                                f"{field} >= '2025-01-01'。"
                                "不确定时按快照处理；业务时间范围请加在业务时间列上，"
                                "不要把快照表的分区列当业务时间。"
                            ),
                        },
                    )
                )
    return names, latest_runtime, problems


def _partition_analysis(tree, project_id=None):
    try:
        tables = (
            knowledge.describe()
            if project_id in (None, "default")
            else knowledge.describe(project_id=project_id)
        )
    except Exception as exc:
        raise _annotate_query_failure(
            HTTPException(503, "无法读取分区元数据"), "table_catalog", "data_source"
        ) from exc
    names, latest_runtime, problems = _partition_issues(tree, tables)
    if problems:
        status, issue = problems[0]
        raise HTTPException(status, issue["message"])
    return names, latest_runtime


def _tables_and_partitions(tree, project_id=None):
    names, _ = _partition_analysis(tree, project_id=project_id)
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
            plan = {}
            if row is not None and row.owner_id == user.id and row.plan_data:
                try:
                    parsed = json.loads(row.plan_data)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    plan = parsed
            if (
                plan.get("source") == "mcp"
                and row.created_at.replace(tzinfo=UTC) < cutoff
            ):
                db.delete(row)


@router.post("/api/v1/queries/plan")
async def plan(body: PlanInput, user: Annotated[Requester, Depends(current_requester)]):
    return await _create_plan(body, user)


async def _create_plan(
    body, user, conversation_id=None, turn_id=None, source=None, project_id=None
):
    tree = _validate_sql(body.sql)
    tables, latest_partition = _partition_analysis(tree, project_id=project_id)
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
        "project_id": project_id or "default",
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


async def propose_query(sql: str, project_id: str | None = None) -> dict:
    """Propose a validated Doris SQL query for the questioner to confirm.

    This creates a plan only; it does not execute the query. After proposing,
    stop and wait for the questioner to confirm execution.
    """
    from . import sql_validation
    from .mcp_context import current_conversation_id, current_requester, current_turn_id
    from .mcp_contract import ToolInvalidInput

    if not isinstance(sql, str) or not sql.strip():
        raise ToolInvalidInput("sql 参数不能为空")
    validation = (
        sql_validation.validate_sql(sql, dialect="doris")
        if project_id in (None, "default")
        else sql_validation.validate_sql(sql, dialect="doris", project_id=project_id)
    )
    if validation.get("status") != "pass":
        details = "; ".join(
            str(issue.get("message") or issue.get("kind"))
            for issue in validation.get("issues", [])
            if isinstance(issue, dict)
        )
        message = "SQL validation did not pass"
        if details:
            message += ": " + details
        raise ToolInvalidInput(message[:4000])
    conversation_id = current_conversation_id()
    supplied_turn = current_turn_id()
    if conversation_id and (
        not supplied_turn
        or not supplied_turn.isascii()
        or not supplied_turn.isdecimal()
    ):
        raise ToolInvalidInput("Active conversation turn not found")
    try:
        return await _create_plan(
            PlanInput(sql=sql),
            current_requester(),
            conversation_id,
            int(supplied_turn) if supplied_turn else None,
            project_id=project_id,
        )
    except HTTPException as exc:
        if 400 <= exc.status_code < 500:
            raise ToolInvalidInput(str(exc.detail)) from exc
        raise


async def execute_query(
    sql: str | None = None,
    confirmation_id: str | None = None,
    project_id: str | None = None,
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
            return await _create_plan(
                PlanInput(sql=sql), user, source="mcp", project_id=project_id
            )
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
            confirmation_id,
            ExecuteInput(),
            user,
            kind="mcp",
            expected_project_id=project_id,
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
    *,
    expected_project_id: str | None = None,
):
    with sessions().begin() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id:
            raise HTTPException(404, "查询不存在")
        saved_plan, project_id = _resolved_plan_project(db, row)
        if expected_project_id is not None and project_id != expected_project_id:
            raise HTTPException(404, "查询不存在")
        require_query_project_access(db, project_id, user)
        plugin, config = _config()
        limit = body.limit if body.limit is not None else config.get("row_limit", 1000)
        if limit > config.get("row_limit", 1000):
            raise HTTPException(422, "超过返回行数上限")
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
    stage, category = "unknown", "unknown"
    try:
        stage = "validate_sql"
        tree = _validate_sql(sql)
        stage = "table_catalog"
        _tables_and_partitions(tree, project_id=project_id)
        cap = min(
            config.get("download_limit", 100000),
            saved_plan.get("execution_limit", config.get("download_limit", 100000)),
        )
        bounded = f"SELECT * FROM ({tree.sql(dialect='doris')}) AS _dataagent_query LIMIT {cap}"
        executor = plugin.bind(config, token)
        async with asyncio.timeout(config.get("timeout_seconds", 300)):
            stage = "submit"
            key = await executor.submit(bounded, cap)
            stage = "wait"
            result = await executor.wait(key)
        stage = "result"
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
        category = "authentication"
        executor_stage = getattr(exc, "failure_stage", None)
        if executor_stage == "submit":
            stage = "submit"
        elif executor_stage == "poll":
            stage = "wait"
        raise _annotate_query_failure(
            HTTPException(401, "DTB token 失效，请在个人设置中更新"),
            stage,
            category,
        ) from None
    except TimeoutError:
        category = "timeout"
        raise _annotate_query_failure(
            HTTPException(504, "查询超时"), stage, category
        ) from None
    except ExecutorError as exc:
        category = getattr(exc, "failure_category", "unknown")
        executor_stage = getattr(exc, "failure_stage", None)
        if executor_stage in {"submit", "poll", "download"}:
            stage = {"submit": "submit", "poll": "wait", "download": "result"}[
                executor_stage
            ]
        raise _annotate_query_failure(
            HTTPException(502, "查询执行失败"), stage, category
        ) from None
    except HTTPException as exc:
        failure_stage = getattr(exc, "failure_stage", None)
        if failure_stage is not None:
            stage = safe_query_stage(failure_stage)
        failure_category = getattr(exc, "failure_category", None)
        category = (
            safe_dtb_category(failure_category)
            if failure_category is not None
            else "unknown"
        )
        if category == "unknown":
            category = "sql" if stage == "validate_sql" else "table_or_catalog"
        detail = {
            "validate_sql": "SQL 校验失败",
            "table_catalog": "查询未通过目录或分区校验",
        }.get(stage, "查询执行失败")
        raise _annotate_query_failure(
            HTTPException(exc.status_code, detail), stage, category
        ) from None
    except Exception:  # noqa: BLE001 - unknown failures degrade to a fixed category
        category = "unknown"
        raise _annotate_query_failure(
            HTTPException(502, "查询执行失败"), stage, category
        ) from None
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
            if key is not None:
                row.query_key = key
            if status != "succeeded":
                _record_execution_failure(db, row, stage, category)


def audit_revoked_token_attempt(
    credentials: HTTPAuthorizationCredentials | None,
    kind: str,
    query_id: str | None = None,
    *,
    stage: str | None = None,
) -> None:
    """Audit an entry rejected because the bearer token was revoked.

    Unknown credentials are not audited: there is no real identity to attach
    the attempt to.  Only the digest is looked up; the credential, digest and
    request body are never stored or logged.  Callers that cannot prove a
    query id, such as the MCP transport, omit it rather than trusting the
    request.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        return
    with sessions().begin() as db:
        token = db.get(Token, digest(credentials.credentials))
        if token is not None:
            if not token.revoked:
                return
            owner_id = token.id
        else:
            scoped = db.get(AgentCredential, digest(credentials.credentials))
            if scoped is None:
                return
            owner = db.query(Token).filter_by(id=scoped.owner_id).first()
            if owner is None or not owner.revoked:
                return
            owner_id = owner.id
        detail = {"entry": kind}
        if query_id is not None:
            detail["query_id"] = query_id
        if stage is not None:
            detail["stage"] = stage
        db.add(
            QueryAudit(
                id=uuid.uuid4().hex,
                owner_id=owner_id,
                sql=json.dumps(detail, ensure_ascii=False),
                kind=kind,
                status="rejected_401",
                row_count=None,
                elapsed_ms=0,
            )
        )


def _audit_mcp_rejection(db, owner_id: str, status_code: int, stage: str) -> None:
    db.add(
        QueryAudit(
            id=uuid.uuid4().hex,
            owner_id=owner_id,
            sql=json.dumps({"entry": "mcp", "stage": stage}, ensure_ascii=False),
            kind="mcp",
            status=f"rejected_{status_code}",
            row_count=None,
            elapsed_ms=0,
        )
    )


def audit_mcp_authentication_rejection(
    credentials: HTTPAuthorizationCredentials | None,
    status_code: int,
    *,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Audit an MCP rejection that happens before tool dispatch.

    The identity always comes from the credential record, never from request
    headers or the body.  A 401 needs a real revoked token (or the revoked
    owner of a real agent credential); a 409 needs an agent credential whose
    stored conversation and turn match the request and whose stored project is
    frozen.  Unknown credentials and forged header values write no audit.  No
    query id can be proven at this stage, so none is stored.
    """
    if status_code not in (401, 409):
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        return
    with sessions().begin() as db:
        token = db.get(Token, digest(credentials.credentials))
        if token is not None:
            if status_code != 401 or not token.revoked:
                return
            owner_id = token.id
        else:
            scoped = db.get(AgentCredential, digest(credentials.credentials))
            if scoped is None:
                return
            if status_code == 409 and (
                scoped.conversation_id != conversation_id
                or str(scoped.turn_id) != turn_id
                or not project_is_frozen(scoped.project_id)
            ):
                return
            owner = db.query(Token).filter_by(id=scoped.owner_id).first()
            if owner is None or (status_code == 401 and not owner.revoked):
                return
            owner_id = owner.id
        _audit_mcp_rejection(db, owner_id, status_code, "authentication")


def audit_mcp_project_rejection(owner_id: str, status_code: int) -> None:
    """Audit a project-resolution rejection for an authenticated MCP caller."""
    if status_code not in (404, 409):
        return
    with sessions().begin() as db:
        if db.query(Token).filter_by(id=owner_id).first() is None:
            return
        _audit_mcp_rejection(db, owner_id, status_code, "project_resolution")


def _query_requester_dependency(kind: str):
    def dependency(
        query_id: str,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Requester:
        try:
            return _authenticated(credentials)
        except HTTPException as exc:
            if exc.status_code == 401:
                try:
                    audit_revoked_token_attempt(credentials, kind, query_id)
                except Exception:
                    logger.exception("failed to audit a revoked token attempt")
            raise

    return dependency


query_requester = _query_requester_dependency("query")
download_requester = _query_requester_dependency("download")
download_link_requester = _query_requester_dependency("download_link_created")


@router.post("/api/v1/queries/{query_id}/execute")
async def execute(
    query_id: str,
    body: ExecuteInput,
    user: Annotated[Requester, Depends(query_requester)],
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
    query_id: str,
    body: ExecuteInput,
    user: Requester,
    kind="query",
    *,
    expected_project_id: str | None = None,
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
        result = await _execute(
            query_id,
            body,
            user,
            audit_id,
            expected_project_id=expected_project_id,
        )
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


_MISSING_PLAN_PROJECT = object()


def _plan_dict(row, *, not_found_detail: str) -> dict:
    if row.plan_data is None:
        return {}
    try:
        plan = json.loads(row.plan_data)
    except (TypeError, ValueError):
        raise HTTPException(404, not_found_detail) from None
    if not isinstance(plan, dict):
        raise HTTPException(404, not_found_detail)
    return plan


def _bound_conversation_project(db, row, *, not_found_detail: str) -> str | None:
    if row.conversation_id is None:
        return None
    conversation = db.get(Conversation, row.conversation_id)
    if conversation is None or conversation.owner_id != row.owner_id:
        raise HTTPException(404, not_found_detail)
    project_id = conversation.project_id
    if (
        not isinstance(project_id, str)
        or not project_id
        or project_id != project_id.strip()
    ):
        raise HTTPException(404, not_found_detail)
    return project_id


def _resolved_plan_project(
    db, row, *, not_found_detail: str = "查询不存在"
) -> tuple[dict, str]:
    """Return the plan's own project, failing closed when it is not provable.

    ``plan_data.project_id`` is the only authorization source (#232).  The
    field arrived with project scoping in #178 (4be73d2); every later writer
    stores it unconditionally.  A plan without it therefore predates project
    scoping, when REST, conversation and MCP plan creation all targeted the
    default project.  That code history is the only compatibility path.

    A bound conversation is an integrity check, never an authorization source.
    A non-default conversation on a plan without ``project_id`` cannot have
    been produced by a known writer, so it is denied instead of being used to
    grant access to the conversation's project.  Invalid JSON, a non-object
    plan, a malformed project id, an unknown source or a conflicting
    conversation also fail closed.
    """
    plan = _plan_dict(row, not_found_detail=not_found_detail)
    planned_project = plan.get("project_id", _MISSING_PLAN_PROJECT)
    if planned_project is not _MISSING_PLAN_PROJECT:
        if (
            not isinstance(planned_project, str)
            or not planned_project
            or planned_project != planned_project.strip()
        ):
            raise HTTPException(404, not_found_detail)
        bound_project = _bound_conversation_project(
            db, row, not_found_detail=not_found_detail
        )
        if bound_project is not None and bound_project != planned_project:
            raise HTTPException(404, not_found_detail)
        return plan, planned_project

    # #81 introduced conversation_id and plan_data together.  A bound row with
    # no plan cannot come from that writer and is treated as damaged.
    if row.plan_data is None and row.conversation_id is not None:
        raise HTTPException(404, not_found_detail)
    if plan.get("source") not in (None, "", "mcp"):
        raise HTTPException(404, not_found_detail)
    bound_project = _bound_conversation_project(
        db, row, not_found_detail=not_found_detail
    )
    if bound_project is not None and bound_project != "default":
        raise HTTPException(404, not_found_detail)
    return plan, "default"


def _plan_project_id(db, row, *, not_found_detail: str = "查询不存在") -> str:
    _, project_id = _resolved_plan_project(db, row, not_found_detail=not_found_detail)
    return project_id


def _require_query_project_access(db, row, user: Requester) -> None:
    """All query access follows the project recorded on the plan itself."""
    require_query_project_access(db, _plan_project_id(db, row), user)


DOWNLOAD_LINK_TTL_SECONDS = 30 * 60
DOWNLOAD_FORMATS = ("csv", "xlsx", "json")


def _download_signature(query_id: str, format: str, expires: int) -> str:
    message = f"{query_id}:{format}:{expires}".encode()
    key = hmac.new(
        os.environ["DATAAGENT_ENCRYPTION_KEY"].encode(),
        b"semantica-dataagent/download-link/v1",
        hashlib.sha256,
    ).digest()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _audit_download(
    owner_id: str, query_id: str, format: str, kind: str, status="succeeded"
) -> None:
    with sessions().begin() as db:
        db.add(
            QueryAudit(
                id=uuid.uuid4().hex,
                owner_id=owner_id,
                sql=json.dumps({"query_id": query_id, "format": format}),
                kind=kind,
                status=status,
                row_count=None,
                elapsed_ms=0,
            )
        )


def _download_links(query_id: str, user: Requester, base: str) -> dict[str, str]:
    _cleanup_expired_conversation_results(query_id, user)
    denial = None
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id or row.status != "succeeded":
            raise HTTPException(404, "查询结果不存在")
        try:
            _require_query_project_access(db, row, user)
        except HTTPException as exc:
            denial = exc
    if denial is not None:
        _audit_download(
            user.id,
            query_id,
            "all",
            "download_link_created",
            f"rejected_{denial.status_code}",
        )
        raise denial
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
    query_id: str, user: Annotated[Requester, Depends(download_link_requester)]
):
    return _download_links(query_id, user, "")


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
    denial = None
    owner_id = None
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None:
            raise HTTPException(404, "查询结果不存在")
        owner_id = row.owner_id
        user = Requester(id=row.owner_id, role="user", token="")
        try:
            _require_query_project_access(db, row, user)
        except HTTPException as exc:
            denial = exc
    if denial is not None:
        _audit_download(
            owner_id,
            query_id,
            format,
            "download_link_used",
            f"rejected_{denial.status_code}",
        )
        raise denial
    response = await download(query_id, user, format)
    _audit_download(user.id, query_id, format, "download_link_used")
    return response


@router.get("/api/v1/queries/{query_id}/download")
async def download(
    query_id: str,
    user: Annotated[Requester, Depends(download_requester)],
    format: Literal["csv", "xlsx", "json"] = "csv",
):
    _cleanup_expired_conversation_results(query_id, user)
    denial = None
    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        if row is None or row.owner_id != user.id or row.status != "succeeded":
            raise HTTPException(404, "查询结果不存在")
        try:
            _require_query_project_access(db, row, user)
        except HTTPException as exc:
            denial = exc
        if denial is None:
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
                    content, content_type = (
                        buffer.getvalue().encode("utf-8"),
                        "text/csv",
                    )
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
                    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
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
    if denial is not None:
        _audit_download(
            user.id,
            query_id,
            format,
            "download",
            f"rejected_{denial.status_code}",
        )
        raise denial
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
