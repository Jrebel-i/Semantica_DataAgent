"""Controlled read-only SQL AST builder and Personal DTB Token execution with QueryAudit (#303)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlglot
from fastapi import HTTPException
from sqlglot import exp

from .crypto import decrypt
from .db import PersonalDtbToken, QueryAudit, sessions
from .dtb_auth import authentication_failed
from .dtb_executor import column_names
from .failure_diagnostics import safe_dtb_category
from .plugins import ExecutorAuthenticationError, ExecutorError
from .project_context import (
    is_valid_sql_identifier,
    is_valid_status_literal,
    is_valid_table_identifier,
)
from .query_api import _config as _executor_config
from .query_api import _validate_sql as _validate_readonly_query

_EXTRA_FORBIDDEN_NODES = tuple(
    node
    for node in (
        getattr(exp, "Transaction", None),
        getattr(exp, "Commit", None),
        getattr(exp, "Rollback", None),
        getattr(exp, "Pragma", None),
        getattr(exp, "Use", None),
        getattr(exp, "Set", None),
    )
    if node is not None
)


class DiscoveryExecutionError(RuntimeError):
    """Sanitized discovery execution error; never exposes raw external platform text or secrets."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 502,
        category: str = "unknown",
    ):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.category = category


class DiscoverySqlValidationError(DiscoveryExecutionError):
    """Raised when a discovery identifier, value, or generated SQL fails safety validation."""

    def __init__(self, detail: str):
        super().__init__(detail, status_code=422, category="invalid_sql")


class DiscoveryTokenMissingError(DiscoveryExecutionError):
    """Raised when the triggering caller has not configured a personal DTB token."""

    def __init__(self, detail: str = "请先在个人设置中保存 DTB token"):
        super().__init__(detail, status_code=400, category="missing_token")


class DiscoveryAuthenticationError(DiscoveryExecutionError):
    """Raised when the caller's personal DTB token is rejected or expired (401)."""

    def __init__(self, detail: str = "DTB token 失效，请在个人设置中更新"):
        super().__init__(detail, status_code=401, category="authentication")


class DiscoveryExecutorUnavailableError(DiscoveryExecutionError):
    """Raised when no valid DTB executor plugin is enabled."""

    def __init__(self, detail: str = "尚未启用执行器"):
        super().__init__(detail, status_code=503, category="executor_unavailable")


class DiscoveryTimeoutError(DiscoveryExecutionError):
    """Raised when a controlled discovery query exceeds its timeout budget."""

    def __init__(self, detail: str = "DTB 查询超时"):
        super().__init__(detail, status_code=504, category="timeout")


@dataclass(frozen=True)
class DiscoveryQueryResult:
    """Result returned by a single audited read-only DTB query."""

    audit_id: str
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    total_rows: int
    elapsed_ms: int


@dataclass(frozen=True)
class QueryHistoryRecord:
    """Normalized raw query-history row prior to SQL scrubbing and fingerprinting."""

    sql: str
    status: str | None = None
    executed_at: str | None = None
    database: str | None = None
    caller: str | None = None
    dialect: str | None = None


@dataclass(frozen=True)
class QueryHistoryFetchResult:
    """Result of fetching query history via the caller's personal DTB token."""

    audit_id: str
    sql: str
    records: list[QueryHistoryRecord]
    raw_row_count: int
    elapsed_ms: int


def validate_sql_identifier(value: Any, label: str = "标识符") -> str:
    """Validate a single SQL column/table segment against the strict identifier allowlist."""
    if not is_valid_sql_identifier(value):
        raise DiscoverySqlValidationError(f"{label}不合法")
    return str(value)


def validate_table_identifier(
    value: Any, label: str = "源表名"
) -> tuple[str | None, str | None, str]:
    """Validate a 1-, 2-, or 3-part SQL table identifier and return (catalog, db, table)."""
    if not is_valid_table_identifier(value):
        raise DiscoverySqlValidationError(f"{label}不合法")
    parts = str(value).split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    return None, None, parts[0]


def validate_status_literal(value: Any) -> str:
    """Validate a status filter literal value against the strict allowlist."""
    if not is_valid_status_literal(value):
        raise DiscoverySqlValidationError("成功状态过滤值不合法")
    return str(value)


def build_table_node(table_identifier: str, *, alias: str | None = None) -> exp.Table:
    """Build a quoted sqlglot Table AST node strictly from allowlisted identifiers."""
    catalog, database, name = validate_table_identifier(table_identifier)
    alias_node = (
        exp.TableAlias(
            this=exp.Identifier(
                this=validate_sql_identifier(alias, "表别名"), quoted=True
            )
        )
        if alias is not None
        else None
    )
    return exp.Table(
        this=exp.Identifier(this=name, quoted=True),
        db=exp.Identifier(this=database, quoted=True) if database else None,
        catalog=exp.Identifier(this=catalog, quoted=True) if catalog else None,
        alias=alias_node,
    )


def build_column_node(
    column_name: str, *, table_alias: str | None = None
) -> exp.Column:
    """Build a quoted sqlglot Column AST node strictly from allowlisted identifiers."""
    col = validate_sql_identifier(column_name, "列名")
    if table_alias is not None:
        tbl = validate_sql_identifier(table_alias, "表别名")
        return exp.column(
            exp.Identifier(this=col, quoted=True),
            table=exp.Identifier(this=tbl, quoted=True),
        )
    return exp.column(exp.Identifier(this=col, quoted=True))


def _validate_int_in_range(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoverySqlValidationError(f"{name} 必须为整数")
    if not (minimum <= value <= maximum):
        raise DiscoverySqlValidationError(f"{name} 超出允许范围 [{minimum}, {maximum}]")
    return value


def ensure_readonly_select_sql(
    sql: str,
    *,
    dialect: str = "doris",
    require_limit: bool = True,
) -> exp.Query:
    """Verify a SQL string is a single read-only SELECT/WITH query (with optional LIMIT)."""
    if not isinstance(sql, str) or not sql.strip():
        raise DiscoverySqlValidationError("SQL 不能为空")
    try:
        tree = _validate_readonly_query(sql)
    except HTTPException as exc:
        raise DiscoverySqlValidationError(str(exc.detail)) from None

    # Double-check with explicit dialect if different from default doris.
    if dialect != "doris":
        try:
            parsed = sqlglot.parse(sql, read=dialect)
        except sqlglot.errors.SqlglotError:
            raise DiscoverySqlValidationError("SQL 无法解析") from None
        if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
            raise DiscoverySqlValidationError("仅允许单一只读 SELECT 查询")
        tree = parsed[0]

    for node in tree.walk():
        if isinstance(
            node,
            (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock)
            + _EXTRA_FORBIDDEN_NODES,
        ):
            raise DiscoverySqlValidationError("仅允许只读 SELECT 查询")

    if require_limit:
        limit_node = tree.args.get("limit")
        if limit_node is None:
            raise DiscoverySqlValidationError("只读查询必须包含明确的 LIMIT 上限")
        limit_expr = limit_node.expression
        if not isinstance(limit_expr, exp.Literal) or limit_expr.is_string:
            raise DiscoverySqlValidationError("LIMIT 必须为正整数常量")
        try:
            limit_val = int(limit_expr.this)
        except (TypeError, ValueError):
            raise DiscoverySqlValidationError("LIMIT 必须为正整数常量") from None
        if limit_val <= 0:
            raise DiscoverySqlValidationError("LIMIT 必须大于 0")
    return tree


def build_query_history_sql(
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
    database_filter: str | Sequence[str] | None = None,
) -> str:
    """Construct a controlled read-only query-history SELECT strictly via sqlglot AST."""
    source_table = config.get("source_table")
    table_node = build_table_node(str(source_table or ""))

    sql_col = validate_sql_identifier(config.get("sql_column"), "SQL 文本列")
    status_col = validate_sql_identifier(config.get("status_column"), "执行状态列")
    time_col = validate_sql_identifier(config.get("time_column"), "时间/分区列")

    raw_db_col = config.get("database_column")
    db_col = (
        validate_sql_identifier(raw_db_col, "库名列")
        if raw_db_col not in (None, "")
        else None
    )
    raw_caller_col = config.get("caller_column")
    caller_col = (
        validate_sql_identifier(raw_caller_col, "调用方列")
        if raw_caller_col not in (None, "")
        else None
    )
    raw_dialect_col = config.get("dialect_column")
    dialect_col = (
        validate_sql_identifier(raw_dialect_col, "方言列")
        if raw_dialect_col not in (None, "")
        else None
    )

    raw_statuses = config.get("success_statuses")
    if not isinstance(raw_statuses, Sequence) or isinstance(raw_statuses, (str, bytes)):
        raise DiscoverySqlValidationError("success_statuses 必须为非空状态列表")
    statuses: list[str] = []
    seen_statuses: set[str] = set()
    for item in raw_statuses:
        validated = validate_status_literal(item)
        if validated not in seen_statuses:
            seen_statuses.add(validated)
            statuses.append(validated)
    if not statuses or len(statuses) > 20:
        raise DiscoverySqlValidationError(
            "success_statuses 必须包含 1 到 20 个合法状态值"
        )

    lookback_days = _validate_int_in_range(
        config.get("lookback_days", 7),
        name="lookback_days",
        minimum=1,
        maximum=90,
    )
    max_queries = _validate_int_in_range(
        config.get("max_queries", 5000),
        name="max_queries",
        minimum=50,
        maximum=20000,
    )

    projections: list[exp.Expression] = [
        exp.alias_(
            build_column_node(sql_col),
            exp.Identifier(this="sql_text", quoted=True),
        ),
        exp.alias_(
            build_column_node(status_col),
            exp.Identifier(this="exec_status", quoted=True),
        ),
        exp.alias_(
            build_column_node(time_col),
            exp.Identifier(this="exec_time", quoted=True),
        ),
    ]
    if db_col is not None:
        projections.append(
            exp.alias_(
                build_column_node(db_col),
                exp.Identifier(this="database_name", quoted=True),
            )
        )
    if caller_col is not None:
        projections.append(
            exp.alias_(
                build_column_node(caller_col),
                exp.Identifier(this="caller", quoted=True),
            )
        )
    if dialect_col is not None:
        projections.append(
            exp.alias_(
                build_column_node(dialect_col),
                exp.Identifier(this="dialect", quoted=True),
            )
        )

    anchor = now if now is not None else datetime.now(UTC)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    else:
        anchor = anchor.astimezone(UTC)
    cutoff_str = (anchor - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")

    query = exp.select(*projections).from_(table_node)
    query = query.where(
        exp.GTE(
            this=build_column_node(time_col),
            expression=exp.Literal.string(cutoff_str),
        )
    )
    if len(statuses) == 1:
        query = query.where(
            exp.EQ(
                this=build_column_node(status_col),
                expression=exp.Literal.string(statuses[0]),
            )
        )
    else:
        query = query.where(
            exp.In(
                this=build_column_node(status_col),
                expressions=[exp.Literal.string(st) for st in statuses],
            )
        )

    if database_filter is not None and db_col is not None:
        raw_dbs = (
            [database_filter]
            if isinstance(database_filter, str)
            else list(database_filter)
        )
        validated_dbs = [validate_sql_identifier(d, "库名过滤") for d in raw_dbs]
        if len(validated_dbs) == 1:
            query = query.where(
                exp.EQ(
                    this=build_column_node(db_col),
                    expression=exp.Literal.string(validated_dbs[0]),
                )
            )
        elif validated_dbs:
            query = query.where(
                exp.In(
                    this=build_column_node(db_col),
                    expressions=[exp.Literal.string(d) for d in validated_dbs],
                )
            )

    query = query.order_by(exp.Ordered(this=build_column_node(time_col), desc=True))
    query = query.limit(max_queries)

    sql = query.sql(dialect="doris")
    ensure_readonly_select_sql(sql, dialect="doris", require_limit=True)
    return sql


def require_personal_dtb_token(owner_id: str) -> str:
    """Decrypt and return the caller's personal DTB token, or raise 400 if absent."""
    with sessions()() as db:
        secret = db.get(PersonalDtbToken, owner_id)
    if secret is None or not secret.encrypted_token:
        raise DiscoveryTokenMissingError()
    try:
        token = decrypt(secret.encrypted_token)
    except Exception:  # noqa: BLE001 - never expose crypto internals
        raise DiscoveryTokenMissingError(
            "个人 DTB token 无法解密，请重新保存"
        ) from None
    if not token:
        raise DiscoveryTokenMissingError()
    return token


def resolve_executor_plugin() -> tuple[Any, dict[str, Any]]:
    """Return the configured and enabled DTB executor plugin, or raise 503."""
    try:
        return _executor_config()
    except HTTPException as exc:
        raise DiscoveryExecutorUnavailableError(str(exc.detail)) from None


def resolve_discovery_dtb_runtime(owner_id: str) -> tuple[Any, dict[str, Any], str]:
    """Validate personal DTB token (400) and configured executor plugin (503)."""
    token = require_personal_dtb_token(owner_id)
    plugin, config = resolve_executor_plugin()
    return plugin, config, token


def _start_audit(owner_id: str, sql: str, kind: str) -> str:
    audit_id = uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            QueryAudit(
                id=audit_id,
                owner_id=owner_id,
                sql=sql,
                kind=kind,
                status="running",
                row_count=None,
                elapsed_ms=0,
            )
        )
    return audit_id


def _finish_audit(
    audit_id: str, status: str, row_count: int | None, elapsed_ms: int
) -> None:
    with sessions().begin() as db:
        record = db.get(QueryAudit, audit_id)
        if record is not None:
            record.status = status
            record.row_count = row_count
            record.elapsed_ms = elapsed_ms


async def execute_readonly_dtb_query(
    *,
    owner_id: str,
    sql: str,
    limit: int,
    kind: str = "discovery_history",
    timeout_seconds: float | None = None,
    runtime: tuple[Any, dict[str, Any], str] | None = None,
    require_limit: bool = True,
) -> DiscoveryQueryResult:
    """Execute one controlled read-only SELECT via caller's personal DTB token with QueryAudit.

    Reusable by both query-history fetching (#303) and data probing (#304).
    Raw external platform errors, credentials, cookies, and stack traces are never propagated.
    """
    ensure_readonly_select_sql(sql, dialect="doris", require_limit=require_limit)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise DiscoverySqlValidationError("limit 必须为正整数")

    plugin, config, token = (
        runtime if runtime is not None else resolve_discovery_dtb_runtime(owner_id)
    )
    config_timeout = float(config.get("timeout_seconds", 300))
    effective_timeout = (
        min(float(timeout_seconds), config_timeout)
        if timeout_seconds is not None and timeout_seconds > 0
        else config_timeout
    )

    started = time.monotonic()
    audit_id = _start_audit(owner_id, sql, kind)
    status = "failed"
    row_count: int | None = None
    try:
        executor = plugin.bind(config, token)
        async with asyncio.timeout(effective_timeout):
            key = await executor.submit(sql, limit)
            result = await executor.wait(key)

        if not isinstance(result, dict):
            raise DiscoveryExecutionError(
                "执行器返回结果格式无效", status_code=502, category="result"
            )
        if authentication_failed(None, result):
            raise DiscoveryAuthenticationError()

        names = column_names(result.get("columns"))
        rows = result.get("data")
        cap = int(config.get("download_limit", 100000))
        if names is None:
            raise DiscoveryExecutionError(
                "执行器返回结果格式无效", status_code=502, category="result"
            )
        if (
            not isinstance(rows, list)
            or len(rows) > cap
            or not all(isinstance(row, list) and len(row) == len(names) for row in rows)
        ):
            raise DiscoveryExecutionError(
                "执行器返回无效结果或超过下载上限",
                status_code=502,
                category="result",
            )

        sliced = rows[:limit]
        status = "succeeded"
        row_count = len(sliced)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return DiscoveryQueryResult(
            audit_id=audit_id,
            sql=sql,
            columns=names,
            rows=sliced,
            total_rows=len(rows),
            elapsed_ms=elapsed_ms,
        )
    except DiscoveryExecutionError:
        raise
    except ExecutorAuthenticationError:
        raise DiscoveryAuthenticationError() from None
    except TimeoutError:
        raise DiscoveryTimeoutError() from None
    except ExecutorError as exc:
        category = safe_dtb_category(getattr(exc, "failure_category", "unknown"))
        raise DiscoveryExecutionError(
            "DTB 查询执行失败", status_code=502, category=category
        ) from None
    except Exception:  # noqa: BLE001 - scrub any unexpected internal/external error
        raise DiscoveryExecutionError(
            "DTB 查询执行失败", status_code=502, category="unknown"
        ) from None
    finally:
        _finish_audit(
            audit_id, status, row_count, int((time.monotonic() - started) * 1000)
        )


def _cell_str(row: Sequence[Any], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = row[index]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _col_index(
    lowered_columns: Sequence[str], preferred: str, fallback: str | None = None
) -> int | None:
    if preferred in lowered_columns:
        return lowered_columns.index(preferred)
    if fallback:
        fb = fallback.lower()
        if fb in lowered_columns:
            return lowered_columns.index(fb)
    return None


async def fetch_query_history(
    *,
    owner_id: str,
    config: Mapping[str, Any],
    now: datetime | None = None,
    database_filter: str | Sequence[str] | None = None,
    runtime: tuple[Any, dict[str, Any], str] | None = None,
) -> QueryHistoryFetchResult:
    """Fetch recent successful query-history rows using controlled AST SQL and caller DTB token."""
    sql = build_query_history_sql(config, now=now, database_filter=database_filter)
    max_queries = int(config.get("max_queries", 5000))
    timeout_seconds = float(config.get("probe_timeout_seconds", 30))
    result = await execute_readonly_dtb_query(
        owner_id=owner_id,
        sql=sql,
        limit=max_queries,
        kind="discovery_history",
        timeout_seconds=timeout_seconds,
        runtime=runtime,
        require_limit=True,
    )

    lowered = [c.lower() for c in result.columns]
    idx_sql = _col_index(lowered, "sql_text", str(config.get("sql_column") or ""))
    idx_status = _col_index(
        lowered, "exec_status", str(config.get("status_column") or "")
    )
    idx_time = _col_index(lowered, "exec_time", str(config.get("time_column") or ""))
    idx_db = _col_index(
        lowered, "database_name", str(config.get("database_column") or "") or None
    )
    idx_caller = _col_index(
        lowered, "caller", str(config.get("caller_column") or "") or None
    )
    idx_dialect = _col_index(
        lowered, "dialect", str(config.get("dialect_column") or "") or None
    )

    allowed_statuses = {
        str(s).strip().lower() for s in (config.get("success_statuses") or ())
    }
    records: list[QueryHistoryRecord] = []
    if idx_sql is not None:
        for row in result.rows:
            raw_sql = _cell_str(row, idx_sql)
            if not raw_sql:
                continue
            row_status = _cell_str(row, idx_status)
            if (
                allowed_statuses
                and row_status is not None
                and row_status.lower() not in allowed_statuses
            ):
                continue
            records.append(
                QueryHistoryRecord(
                    sql=raw_sql,
                    status=row_status,
                    executed_at=_cell_str(row, idx_time),
                    database=_cell_str(row, idx_db),
                    caller=_cell_str(row, idx_caller),
                    dialect=_cell_str(row, idx_dialect),
                )
            )

    return QueryHistoryFetchResult(
        audit_id=result.audit_id,
        sql=result.sql,
        records=records,
        raw_row_count=len(result.rows),
        elapsed_ms=result.elapsed_ms,
    )
