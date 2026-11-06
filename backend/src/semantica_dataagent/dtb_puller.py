"""DTB metadata puller."""

import csv
import io
import json
import re
import tempfile
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

import httpx
import sqlglot
from sqlglot import exp

from .dtb_auth import authentication_failed

TASK_SQL = "select version_id,\nversion_code,message,committer,task_id,task_name,\n(case when task_type_id=1 then 'shell'\nwhen task_type_id=2 then 'merge onself'\nwhen task_type_id=3 then 'spark sql'\nelse '' end) as task_type\n,task_conf,task_src,project_name,upstream_tasks,output_table_name,a.create_time,a.modify_time\n from(\n    select *,rank() over(partition by task_id order by modify_time desc) rk  from mysql.data_tool_box_emr.task_version\n    where status!=1 and task_type_id!=4\n) a\nleft join  mysql.data_tool_box_emr.project b\non a.project_id=b.project_id\nwhere rk=1 and status=2"
TABLE_SQL = "select a.*,b.column_name,b.seq_no,b.column_type,b.column_comment,b.column_business_comment from\n(\n    select table_id,table_name,database_name,create_time,modify_time,\n    comment,output_task_id,output_task_name,use_direction\n    from mysql.data_tool_box_emr.metadata_table where catalog_type='HIVE' and status='在用'\n) a\n left join mysql.data_tool_box_emr.metadata_column b on a.table_id=b.table_id"
DIMENSIONS_METADATA_SQL = (
    "SELECT a.table_id, a.table_name, a.database_name, a.create_time, a.modify_time, "
    "a.comment, a.output_task_id, a.output_task_name, a.use_direction, "
    "b.column_name, b.seq_no, b.column_type, b.column_comment, b.column_business_comment "
    "FROM ("
    "SELECT table_id, table_name, database_name, create_time, modify_time, comment, "
    "output_task_id, output_task_name, use_direction "
    "FROM mysql.data_tool_box_emr.metadata_table "
    "WHERE catalog_type='HIVE' AND status='在用' AND database_name='dimensions'"
    ") a LEFT JOIN mysql.data_tool_box_emr.metadata_column b ON a.table_id=b.table_id "
    "ORDER BY a.table_id, b.seq_no"
)
PARTITION_SEQ_NO = 10001
MAX_ROW_LIMIT = 500000


def _partition_stats_sql(names=None):
    selected = f" AND table_name IN ({names})" if names is not None else ""
    return (
        "select * from ("
        "select *,rank() over(partition by database_name,table_name "
        "order by split_part(partition_name, '=', 2) DESC) as rk from("
        "select a.*,b.partition_id,b.partition_name,b.records,"
        "b.create_time as partition_create_time from ("
        "select table_id,table_name,database_name,create_time,modify_time,comment "
        "from mysql.data_tool_box_emr.metadata_table "
        "where catalog_type='HIVE' and status='在用' and database_name='dimensions'"
        f"{selected}"
        ") a left join mysql.data_tool_box_emr.metadata_partition b on a.table_id=b.table_id"
        ") tt) kk where rk=1"
    )


def _result_count(data, key):
    """Read an optional non-negative integer result count."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid DTB query result")  # noqa: TRY004
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid DTB query result") from exc
    if count < 0:
        raise ValueError("Invalid DTB query result")
    return count


def _downloaded_row_count(content, format):
    """Count rows in a downloaded query result when no total was reported."""
    if format == "json":
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError  # noqa: TRY004 - response contents
            total = _result_count(payload, "totalCount")
            if total is not None:
                return total
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise ValueError  # noqa: TRY004 - response contents
            return len(rows)
        except (UnicodeError, TypeError, ValueError) as exc:
            raise ValueError("Invalid DTB query result") from exc
    if format == "csv":
        try:
            reader = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""))
            rows = list(reader)
        except (UnicodeError, csv.Error) as exc:
            raise ValueError("Invalid DTB query result") from exc
        return max(len(rows) - 1, 0)
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            return max((book.active.max_row or 0) - 1, 0)
        finally:
            book.close()
    except Exception as exc:
        raise ValueError("Invalid DTB query result") from exc


def _optional_text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid DTB metadata value")  # noqa: TRY004
    return str(value)


def _integer(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Invalid DTB metadata value")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?[0-9]+", text):
            return int(text)
    raise ValueError("Invalid DTB metadata value")


def checked_request(client, method, base_url, token, path, **kwargs):
    allowed_get = (
        re.fullmatch(r"/api/data-query/(?:result|download)/[A-Za-z0-9_-]+", path)
        or path == "/api/data-query/download"
    )
    if method == "POST" and path == "/api/data-query/async-execute":
        body = kwargs.get("json")
        if (
            not isinstance(body, dict)
            or body.get("dataSource") != "doris_ai"
            or not isinstance(body.get("defaultLimit"), int)
            or isinstance(body.get("defaultLimit"), bool)
            or not 0 <= body["defaultLimit"] <= MAX_ROW_LIMIT
        ):
            raise ValueError("DTB query is not permitted")
        try:
            statements = sqlglot.parse(body.get("sql", ""), read="presto")
        except (sqlglot.errors.ParseError, TypeError) as exc:
            raise ValueError("DTB query is not permitted") from exc
        if (
            len(statements) != 1
            or not isinstance(statements[0], exp.Query)
            or any(
                isinstance(
                    node,
                    (
                        exp.Insert,
                        exp.Update,
                        exp.Delete,
                        exp.Command,
                        exp.Create,
                        exp.Drop,
                    ),
                )
                for node in statements[0].walk()
            )
        ):
            raise ValueError("DTB query is not permitted")
    elif method != "GET" or not allowed_get:
        raise ValueError("DTB endpoint is not permitted")
    headers = {
        "token": token,
        "x-requested-with": "XMLHttpRequest",
        "cookie": f"satoken={token}",
    }
    return client.request(
        method, base_url.rstrip("/") + path, headers=headers, **kwargs
    )


class DtbAuthenticationError(ValueError):
    """The DTB gateway rejected the token."""


class TableFailure(ValueError):
    """One dimension table cannot be pulled without blocking the others."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class DtbPuller:
    id = "dtb"
    version = "1"
    display_name = "DTB"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "token": {"type": "string", "format": "password"},
            "interval_seconds": {"type": "integer", "minimum": 0},
            "layer": {"type": "string", "enum": ["dimensions"]},
            "max_tables": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    }

    @staticmethod
    def _get(client, base_url, token, endpoint, params=None):
        try:
            response = checked_request(
                client, "GET", base_url, token, endpoint, params=params
            )
            if authentication_failed(response.status_code, None):
                raise DtbAuthenticationError(
                    f"DTB authentication failed ({response.status_code})"
                )
            response.raise_for_status()
            payload = response.json()
            if authentication_failed(response.status_code, payload):
                raise DtbAuthenticationError(
                    f"DTB authentication failed ({payload.get('status')})"
                )
            if str(payload.get("status")) != "200" or not isinstance(
                payload.get("data"), dict
            ):
                raise ValueError("DTB request was rejected")
            return payload["data"]
        except httpx.TimeoutException as exc:
            raise ValueError("DTB request timeout") from exc
        except (httpx.HTTPError, TypeError, AttributeError) as exc:
            raise ValueError("DTB request failed") from exc

    @staticmethod
    def _dimension_name(table):
        value = table.get("tableName") if isinstance(table, dict) else table
        if not isinstance(value, str):
            raise ValueError(  # noqa: TRY004 - response contents, not an argument
                "Invalid DTB dimension table name"
            )
        name = value.removeprefix("dimensions.")
        if "." in name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid DTB dimension table name")
        return name

    def fetch_dimensions_metadata(self, client, base_url, token):
        """Read every dimensions table and column from the fixed metadata query."""
        raw = self.query_download(
            client, base_url, token, DIMENSIONS_METADATA_SQL, "json", row_limit=0
        )
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError  # noqa: TRY004 - response contents
            columns, rows = payload.get("columns"), payload.get("data")
            if not isinstance(columns, list) or not isinstance(rows, list):
                raise ValueError  # noqa: TRY004 - response contents
            names = []
            for column in columns:
                if not isinstance(column, dict) or not isinstance(
                    column.get("name"), str
                ):
                    raise ValueError  # noqa: TRY004 - response contents
                names.append(column["name"])
            required = {
                "table_id",
                "table_name",
                "database_name",
                "column_name",
                "seq_no",
                "column_type",
                "column_comment",
                "column_business_comment",
            }
            if not required.issubset(names):
                raise ValueError("Invalid DTB dimensions metadata")
            grouped = {}
            for row in rows:
                if not isinstance(row, list) or len(row) < len(names):
                    raise ValueError("Invalid DTB dimensions metadata")
                values = dict(zip(names, row))
                identity = values.get("table_id")
                if (
                    isinstance(identity, bool)
                    or not isinstance(identity, (str, int))
                    or not str(identity)
                ):
                    raise ValueError("Invalid DTB dimensions metadata")
                name = self._dimension_name(values.get("table_name"))
                key = str(identity)
                table = grouped.get(key)
                if table is None:
                    table = {
                        "tableId": key,
                        "tableName": name,
                        "database": _optional_text(values.get("database_name")),
                        "columns": [],
                        "error": None,
                        "_seen": set(),
                    }
                    grouped[key] = table
                elif table["tableName"] != name:
                    table["error"] = "invalid_table_metadata"
                if table["database"] != "dimensions" and table["error"] is None:
                    table["error"] = "invalid_table_metadata"
                if table["error"] is not None:
                    continue
                column_name = values.get("column_name")
                seq_no = values.get("seq_no")
                column_type = values.get("column_type")
                if column_name is None and seq_no is None and column_type is None:
                    continue
                if (
                    not isinstance(column_name, str)
                    or not column_name
                    or not isinstance(column_type, str)
                    or not column_type
                ):
                    table["error"] = "invalid_column_metadata"
                    continue
                try:
                    ordinal = _integer(seq_no)
                    comment = _optional_text(values.get("column_comment"))
                    business = _optional_text(values.get("column_business_comment"))
                except ValueError:
                    table["error"] = "invalid_column_metadata"
                    continue
                if column_name in table["_seen"]:
                    table["error"] = "duplicate_column_metadata"
                    continue
                table["_seen"].add(column_name)
                table["columns"].append(
                    {
                        "name": column_name,
                        "seq_no": ordinal,
                        "data_type": column_type,
                        "comment": comment,
                        "business_comment": business,
                    }
                )
            result = []
            for table in grouped.values():
                table["columns"].sort(key=lambda column: column["seq_no"])
                if not table["columns"] and table["error"] is None:
                    table["error"] = "missing_columns_metadata"
                table.pop("_seen")
                result.append(table)
            return result
        except (UnicodeError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid DTB dimensions metadata") from exc

    @staticmethod
    def _partition_stats(raw):
        try:
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
            required = {
                "table_name",
                "database_name",
                "partition_name",
                "records",
                "rk",
            }
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError("Invalid DTB partition statistics")
            stats, errors = {}, {}
            for stat in reader:
                name = DtbPuller._dimension_name(stat["table_name"])
                if name in stats or name in errors:
                    stats.pop(name, None)
                    errors[name] = "invalid_partition_statistics"
                    continue
                if stat["database_name"] != "dimensions":
                    errors[name] = "invalid_partition_statistics"
                    continue
                if not stat["partition_name"]:
                    stats[name] = None
                    continue
                field, sep, value = stat["partition_name"].partition("=")
                if (
                    not sep
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field)
                    or not value
                    or stat["rk"] != "1"
                ):
                    errors[name] = "invalid_partition_statistics"
                    continue
                try:
                    count = int(stat["records"])
                except (TypeError, ValueError):
                    errors[name] = "invalid_partition_statistics"
                    continue
                if count < 0:
                    errors[name] = "invalid_partition_statistics"
                    continue
                stats[name] = {"field": field, "value": value, "records": count}
            return stats, errors
        except (UnicodeError, csv.Error, KeyError, TypeError) as exc:
            raise ValueError("Invalid DTB partition statistics") from exc

    def query_download(
        self,
        client,
        base_url,
        token,
        sql,
        format,
        poll_interval=2,
        max_polls=30,
        data_source="presto-emr",
        row_limit=0,
        allow_truncation=False,
    ):
        if format not in {"json", "xlsx", "csv"}:
            raise ValueError("Invalid DTB query format")
        if (
            isinstance(row_limit, bool)
            or not isinstance(row_limit, int)
            or not 0 <= row_limit <= MAX_ROW_LIMIT
        ):
            raise ValueError("Invalid DTB query row limit")
        try:
            response = checked_request(
                client,
                "POST",
                base_url,
                token,
                "/api/data-query/async-execute",
                json={
                    "env": 2,
                    "dataSource": "doris_ai",
                    "sql": sql,
                    "defaultLimit": row_limit,
                },
            )
            if authentication_failed(response.status_code, None):
                raise DtbAuthenticationError(
                    f"DTB authentication failed ({response.status_code})"
                )
            response.raise_for_status()
            payload = response.json()
            if authentication_failed(response.status_code, payload):
                raise DtbAuthenticationError(
                    f"DTB authentication failed ({payload.get('status')})"
                )
            if str(payload.get("status")) != "200":
                raise ValueError("DTB query submission failed")
            data = payload["data"]
            key = data.get("queryKey") or data.get("queryId")
            if not isinstance(key, str) or not key:
                raise ValueError("DTB query missing key")
            key = quote(key, safe="")
            total = None
            for attempt in range(max_polls):
                result = self._get(
                    client, base_url, token, "/api/data-query/result/" + key
                )
                if result.get("errorMessage"):
                    raise ValueError("DTB query failed")
                if result.get("progress") == 1.0:
                    total = _result_count(result, "totalCount")
                    break
                if attempt + 1 < max_polls:
                    time.sleep(poll_interval)
            else:
                raise ValueError("DTB query timed out")
            if (
                row_limit
                and not allow_truncation
                and total is not None
                and total >= row_limit
            ):
                raise ValueError("DTB result truncated")
            response = checked_request(
                client,
                "GET",
                base_url,
                token,
                "/api/data-query/download/" + key,
                params={"format": format},
            )
            if authentication_failed(response.status_code, None):
                raise DtbAuthenticationError(
                    f"DTB authentication failed ({response.status_code})"
                )
            response.raise_for_status()
            if "json" in response.headers.get("content-type", "").lower():
                try:
                    payload = response.json()
                except ValueError:
                    pass
                else:
                    if authentication_failed(response.status_code, payload):
                        raise DtbAuthenticationError(
                            f"DTB authentication failed ({payload.get('status')})"
                        )
            if row_limit and not allow_truncation and total is None:
                total = _downloaded_row_count(response.content, format)
                if total >= row_limit:
                    raise ValueError("DTB result truncated")
            return response.content
        except httpx.TimeoutException as exc:
            raise ValueError("DTB query timeout") from exc
        except (httpx.HTTPError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("DTB query request failed") from exc

    def _fetch_dimension_table(self, client, base_url, token, table, stats, maximum):
        name = table["tableName"]
        columns = table["columns"]
        partition_columns = [
            column for column in columns if column["seq_no"] >= PARTITION_SEQ_NO
        ]
        if len(partition_columns) > 1:
            raise TableFailure("multiple_partition_fields")
        stat = stats.get(name)
        where = ""
        estimated = None
        if partition_columns:
            field = partition_columns[0]["name"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
                raise TableFailure("invalid_partition_field")
            if stat is None:
                raw = self.query_download(
                    client, base_url, token,
                    f"SELECT MAX({field}) AS __partition FROM hive.dimensions.{name}",
                    "json", data_source="doris_query", row_limit=1, allow_truncation=True,
                )
                try:
                    payload = json.loads(raw)
                    value = payload["data"][0][0]
                    if not isinstance(value, str) or not value:
                        raise ValueError("Empty partition")
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    raise TableFailure("missing_latest_partition") from exc
            else:
                if stat["field"] != field:
                    raise TableFailure("partition_stats_field_mismatch")
                value = stat["value"]
                estimated = stat["records"]
            escaped = value.replace("'", "''")
            where = f" WHERE {field} = '{escaped}'"
        elif stat is not None:
            raise TableFailure("partition_stats_field_mismatch")
        dimension_limit = 100 if maximum is not None else 50000
        content = self.query_download(
            client,
            base_url,
            token,
            f"SELECT * FROM hive.dimensions.{name}{where} LIMIT {dimension_limit}",
            "json",
            data_source="doris_query",
            row_limit=dimension_limit,
            allow_truncation=True,
        )
        try:
            payload = json.loads(content)
            descriptors = payload["columns"]
            data = payload["data"]
            if not isinstance(descriptors, list) or not descriptors or not isinstance(data, list):
                raise ValueError("Invalid dimension result")
            names = [column["name"] for column in descriptors]
            if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
                raise ValueError("Invalid dimension columns")
            if any(not isinstance(row, list) or len(row) != len(names) or any(value is not None and not isinstance(value, str) for value in row) for row in data):
                raise ValueError("Invalid dimension rows")
            rows = [dict(zip(names, row, strict=True)) for row in data]
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise TableFailure("invalid_dimension_rows") from exc
        if stat and len(rows) != min(stat["records"], dimension_limit):
            raise TableFailure("dimension_row_count_mismatch")
        meta = {
            "database": "dimensions",
            "table": name,
            "qualified_table": f"dimensions.{name}",
            "columns": [
                {
                    "name": column["name"],
                    "data_type": column["data_type"],
                    "comment": column["comment"],
                    "seq_no": column["seq_no"],
                    "business_comment": column["business_comment"],
                }
                for column in columns
            ],
            "record_count": len(rows),
            "partition_field": field if partition_columns else None,
            "latest_partition": value if partition_columns else None,
            "estimated_rows": estimated,
            "truncated": len(rows) >= dimension_limit,
        }
        return {
            f"dimensions/{name}/meta.json": json.dumps(
                meta, ensure_ascii=False
            ).encode(),
            f"dimensions/{name}/current/rows.jsonl": b"".join(
                (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows
            ),
        }

    def fetch(self, client, base_url, token, parameters=None):
        from .dtb_source import DtbAdapter

        parameters = parameters or {}
        layer = parameters.get("layer", "dimensions")
        maximum = parameters.get("max_tables")
        if layer != "dimensions" or (
            maximum is not None
            and (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 1 <= maximum <= 100
            )
        ):
            raise ValueError("Invalid pull scope")
        tables = self.fetch_dimensions_metadata(client, base_url, token)
        tables.sort(key=lambda table: table["tableName"])
        if maximum is not None:
            tables = tables[:maximum]
        names = ",".join("'" + table["tableName"] + "'" for table in tables)
        names = names or "''"
        task_sql = TASK_SQL
        table_sql = TABLE_SQL
        if maximum is not None:
            task_sql += f" AND output_table_name IN ({names})"
            table_sql += (
                f" WHERE a.database_name='dimensions' AND a.table_name IN ({names})"
            )
        files = {
            "任务信息.json": self.query_download(
                client, base_url, token, task_sql, "json", row_limit=0
            ),
            "表信息.xlsx": self.query_download(
                client, base_url, token, table_sql, "xlsx", row_limit=0
            ),
        }
        stats, stats_errors = {}, {}
        if tables:
            raw = self.query_download(
                client,
                base_url,
                token,
                _partition_stats_sql(names if maximum is not None else None),
                "csv",
                row_limit=0,
            )
            stats, stats_errors = self._partition_stats(raw)
        failures = []
        succeeded = 0
        for table in tables:
            name = table["tableName"]
            reason = table["error"] or stats_errors.get(name)
            if reason is not None:
                failures.append({"table": name, "reason": reason})
                continue
            try:
                files.update(
                    self._fetch_dimension_table(
                        client, base_url, token, table, stats, maximum
                    )
                )
            except DtbAuthenticationError:
                raise
            except TableFailure as exc:
                failures.append({"table": name, "reason": exc.reason})
            except Exception:  # noqa: BLE001 - one table must not stop the pull
                failures.append({"table": name, "reason": "dimension_fetch_failed"})
            else:
                succeeded += 1
        if tables and not succeeded:
            detail = "; ".join(
                f"{failure['table']} ({failure['reason']})" for failure in failures
            )
            raise ValueError(f"All DTB dimension tables failed: {detail}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                dest = root / "dtb" / "snapshot" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            result = DtbAdapter().read(
                root, {"dimensions_path": "dtb/snapshot/dimensions"}
            )
            counts = {
                "tables": sum(r.kind == "table" for r in result.records),
                "tasks": sum(r.kind == "task" for r in result.records),
                "failures": failures,
            }
            if result.stats.get("table_rows_skipped") or result.stats.get(
                "task_rows_unreadable"
            ):
                raise ValueError("Invalid DTB metadata rows")
        return files, counts
