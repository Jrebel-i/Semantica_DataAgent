"""DTB metadata puller."""

import csv
import io
import json
import math
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
DIMENSION_TABLES_SQL = (
    "SELECT table_id, table_name FROM mysql.data_tool_box_emr.metadata_table "
    "WHERE database_name='dimensions' AND catalog_type='HIVE' AND status='在用' "
    "ORDER BY table_name"
)
METADATA_ROW_LIMIT = 500000


def _total_count(data):
    """Read a DTB page total, accepting both ``total`` and ``totalElements``."""
    total = data.get("totalElements")
    if total is None:
        total = data.get("total")
    if total is None:
        return None
    if isinstance(total, bool):
        raise ValueError("Invalid DTB pagination")  # noqa: TRY004
    try:
        total = int(total)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid DTB pagination") from exc
    if total < 0:
        raise ValueError("Invalid DTB pagination")
    return total


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


def checked_request(client, method, base_url, token, path, **kwargs):
    allowed_get = (
        path == "/api/data-map/table"
        or re.fullmatch(r"/api/data-map/detail/[A-Za-z0-9_-]+(?:/column)?", path)
        or re.fullmatch(r"/api/data-query/(?:result|download)/[A-Za-z0-9_-]+", path)
        or path == "/api/data-query/download"
    )
    if method == "POST" and path == "/api/data-query/async-execute":
        body = kwargs.get("json")
        if (
            not isinstance(body, dict)
            or body.get("dataSource") != "doris_ai"
            or not isinstance(body.get("defaultLimit"), int)
            or isinstance(body.get("defaultLimit"), bool)
            or not 0 <= body["defaultLimit"] <= METADATA_ROW_LIMIT
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
                raise ValueError(f"DTB authentication failed ({response.status_code})")
            response.raise_for_status()
            payload = response.json()
            if authentication_failed(response.status_code, payload):
                raise ValueError(f"DTB authentication failed ({payload.get('status')})")
            if str(payload.get("status")) != "200" or not isinstance(
                payload.get("data"), dict
            ):
                raise ValueError("DTB request was rejected")
            return payload["data"]
        except httpx.TimeoutException as exc:
            raise ValueError("DTB request timeout") from exc
        except (httpx.HTTPError, TypeError, AttributeError) as exc:
            raise ValueError("DTB request failed") from exc

    def _pages(self, client, base_url, token, endpoint, key, identity, params, limit):
        rows, seen = [], set()
        page, expected = 1, None
        while True:
            data = self._get(
                client,
                base_url,
                token,
                endpoint,
                {**params, "page": page, "limit": limit},
            )
            try:
                batch = data[key]
                total = _total_count(data)
                if not isinstance(batch, list):
                    raise ValueError("Invalid DTB pagination")  # noqa: TRY004
                if total is not None and page > math.ceil(total / limit) + 1:
                    raise ValueError("Invalid DTB pagination")
                if expected is not None and total is not None and total != expected:
                    raise ValueError("Inconsistent DTB pagination")
                expected = total
                for row in batch:
                    if not isinstance(row, dict) or not isinstance(
                        row.get(identity), (str, int)
                    ):
                        raise ValueError("Invalid DTB row")  # noqa: TRY004
                    value = str(row[identity])
                    if value in seen:
                        raise ValueError("Duplicate DTB row")
                    seen.add(value)
                rows.extend(batch)
                if total is not None:
                    if len(rows) == total:
                        return rows
                    if not batch or len(rows) > total:
                        raise ValueError("Incomplete DTB pagination")
                elif not batch or len(batch) < limit:
                    return rows
                page += 1
            except (KeyError, TypeError) as exc:
                raise ValueError("Invalid DTB pagination") from exc

    @staticmethod
    def _dimension_name(table):
        name = table.get("tableName")
        if not isinstance(name, str):
            raise ValueError(  # noqa: TRY004 - response contents, not an argument
                "Invalid DTB dimension table name"
            )
        name = name.removeprefix("dimensions.")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid DTB dimension table name")
        return name

    def list_tables(self, client, base_url, token, maximum=None):
        if maximum is not None and (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= 100
        ):
            raise ValueError("Invalid pull scope")
        sql = DIMENSION_TABLES_SQL
        if maximum is not None:
            sql += f" LIMIT {maximum}"
        raw = self.query_download(
            client, base_url, token, sql, "json", row_limit=METADATA_ROW_LIMIT
        )
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Invalid DTB dimension tables")  # noqa: TRY004
            columns, rows = payload.get("columns"), payload.get("data")
            if not isinstance(columns, list) or not isinstance(rows, list):
                raise ValueError("Invalid DTB dimension tables")  # noqa: TRY004
            positions = {}
            for position, column in enumerate(columns):
                if not isinstance(column, dict) or not isinstance(
                    column.get("name"), str
                ):
                    raise ValueError(  # noqa: TRY004 - response contents
                        "Invalid DTB dimension tables"
                    )
                positions[column["name"]] = position
            if "table_id" not in positions or "table_name" not in positions:
                raise ValueError("Invalid DTB dimension tables")
            tables = []
            for row in rows:
                if not isinstance(row, list) or len(row) <= max(positions.values()):
                    raise ValueError("Invalid DTB dimension tables")
                identity = row[positions["table_id"]]
                if (
                    isinstance(identity, bool)
                    or not isinstance(identity, (str, int))
                    or not str(identity)
                ):
                    raise ValueError("Invalid DTB dimension tables")
                table = {
                    "tableId": identity,
                    "tableName": row[positions["table_name"]],
                }
                self._dimension_name(table)
                tables.append(table)
            if maximum is not None and len(tables) > maximum:
                raise ValueError("Invalid DTB dimension tables")
            return tables
        except (UnicodeError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid DTB dimension tables") from exc

    def table_detail(self, client, base_url, token, table_id):
        endpoint = "/api/data-map/detail/" + quote(str(table_id), safe="")
        data = self._get(client, base_url, token, endpoint)
        detail = data.get("table", data)
        if not isinstance(detail, dict):
            raise ValueError("Invalid DTB table detail")  # noqa: TRY004
        columns = self._pages(
            client,
            base_url,
            token,
            endpoint + "/column",
            "columnList",
            "columnName",
            {},
            200,
        )
        return detail, columns

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
        row_limit=100,
        allow_truncation=False,
    ):
        if format not in {"json", "xlsx", "csv"}:
            raise ValueError("Invalid DTB query format")
        if (
            isinstance(row_limit, bool)
            or not isinstance(row_limit, int)
            or not 1 <= row_limit <= METADATA_ROW_LIMIT
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
                raise ValueError(f"DTB authentication failed ({response.status_code})")
            response.raise_for_status()
            payload = response.json()
            if authentication_failed(response.status_code, payload):
                raise ValueError(f"DTB authentication failed ({payload.get('status')})")
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
            if not allow_truncation and total is not None and total >= row_limit:
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
                raise ValueError(f"DTB authentication failed ({response.status_code})")
            response.raise_for_status()
            if "json" in response.headers.get("content-type", "").lower():
                try:
                    payload = response.json()
                except ValueError:
                    pass
                else:
                    if authentication_failed(response.status_code, payload):
                        raise ValueError(
                            f"DTB authentication failed ({payload.get('status')})"
                        )
            if not allow_truncation and total is None:
                total = _downloaded_row_count(response.content, format)
                if total >= row_limit:
                    raise ValueError("DTB result truncated")
            return response.content
        except httpx.TimeoutException as exc:
            raise ValueError("DTB query timeout") from exc
        except (httpx.HTTPError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("DTB query request failed") from exc

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
        tables = self.list_tables(client, base_url, token, maximum)
        names = ",".join("'" + self._dimension_name(table) + "'" for table in tables)
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
                client,
                base_url,
                token,
                task_sql,
                "json",
                row_limit=METADATA_ROW_LIMIT,
            ),
            "表信息.xlsx": self.query_download(
                client,
                base_url,
                token,
                table_sql,
                "xlsx",
                row_limit=METADATA_ROW_LIMIT,
            ),
        }
        stats = {}
        if tables:
            sql = (
                "SELECT * FROM (SELECT *, rank() OVER (PARTITION BY database_name, table_name "
                "ORDER BY split_part(partition_name, '=', 2) DESC) AS rk FROM ("
                "SELECT a.table_name, a.database_name, b.partition_name, b.records "
                "FROM mysql.data_tool_box_emr.metadata_table a LEFT JOIN "
                "mysql.data_tool_box_emr.metadata_partition b ON a.table_id=b.table_id "
                "WHERE a.catalog_type='HIVE' AND a.status='在用' AND a.database_name='dimensions' "
                f"AND a.table_name IN ({names})) tt) kk WHERE rk=1"
            )
            raw = self.query_download(
                client,
                base_url,
                token,
                sql,
                "csv",
                row_limit=METADATA_ROW_LIMIT,
            )
            try:
                reader = csv.DictReader(
                    io.StringIO(raw.decode("utf-8-sig"), newline="")
                )
                if not reader.fieldnames or not {
                    "table_name",
                    "database_name",
                    "partition_name",
                    "records",
                    "rk",
                }.issubset(reader.fieldnames):
                    raise ValueError("Invalid DTB partition statistics")
                for stat in reader:
                    name = stat["table_name"].removeprefix("dimensions.")
                    if stat["database_name"] != "dimensions" or name in stats:
                        raise ValueError("Invalid DTB partition statistics")
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
                        raise ValueError("Invalid DTB partition statistics")
                    count = int(stat["records"])
                    if count < 0:
                        raise ValueError("Invalid DTB partition count")
                    stats[name] = (field, value, count)
            except (UnicodeError, csv.Error, KeyError, TypeError) as exc:
                raise ValueError("Invalid DTB partition statistics") from exc
        for table in tables:
            name = self._dimension_name(table)
            _detail, columns = self.table_detail(
                client, base_url, token, table["tableId"]
            )
            if name not in stats:
                raise ValueError("Missing DTB partition statistics")
            stat = stats[name]
            where = ""
            if stat:
                field, value, _estimated = stat
                if field not in {col["columnName"] for col in columns}:
                    raise ValueError("DTB partition field mismatch")
                escaped = value.replace("'", "''")
                where = f" WHERE {field} = '{escaped}'"
            dimension_limit = 100 if maximum is not None else 50000
            content = self.query_download(
                client,
                base_url,
                token,
                f"SELECT * FROM hive.dimensions.{name}{where} LIMIT {dimension_limit}",
                "csv",
                data_source="doris_query",
                row_limit=dimension_limit,
                allow_truncation=True,
            )
            try:
                reader = csv.DictReader(
                    io.StringIO(content.decode("utf-8-sig"), newline="")
                )
                if not reader.fieldnames or any(
                    not value for value in reader.fieldnames
                ):
                    raise ValueError("Invalid DTB dimension columns")
                rows = list(reader)
                if any(None in row for row in rows):
                    raise ValueError("Invalid DTB dimension rows")
            except (UnicodeError, csv.Error) as exc:
                raise ValueError("Invalid DTB dimension download") from exc
            if stat and len(rows) != min(stat[2], dimension_limit):
                raise ValueError("DTB dimension row count mismatch")
            meta = {
                "database": "dimensions",
                "table": name,
                "qualified_table": f"dimensions.{name}",
                "columns": [
                    {
                        "name": col["columnName"],
                        "data_type": col.get("columnType"),
                        "comment": col.get("comment"),
                    }
                    for col in columns
                ],
                "record_count": len(rows),
                "partition_field": stat[0] if stat else None,
                "latest_partition": stat[1] if stat else None,
                "estimated_rows": stat[2] if stat else None,
                "truncated": len(rows) >= dimension_limit,
            }
            files[f"dimensions/{name}/meta.json"] = json.dumps(
                meta, ensure_ascii=False
            ).encode()
            files[f"dimensions/{name}/current/rows.jsonl"] = b"".join(
                (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows
            )
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
            }
            if result.stats.get("table_rows_skipped") or result.stats.get(
                "task_rows_unreadable"
            ):
                raise ValueError("Invalid DTB metadata rows")
        return files, counts
