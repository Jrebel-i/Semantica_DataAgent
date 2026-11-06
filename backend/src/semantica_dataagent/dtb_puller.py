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

TASK_SQL = "select version_id,\nversion_code,message,committer,task_id,task_name,\n(case when task_type_id=1 then 'shell'\nwhen task_type_id=2 then 'merge onself'\nwhen task_type_id=3 then 'spark sql'\nelse '' end) as task_type\n,task_conf,task_src,project_name,upstream_tasks,output_table_name,a.create_time,a.modify_time\n from(\n    select *,rank() over(partition by task_id order by modify_time desc) rk  from mysql.data_tool_box_emr.task_version\n    where status!=1 and task_type_id!=4\n) a\nleft join  mysql.data_tool_box_emr.project b\non a.project_id=b.project_id\nwhere rk=1 and status=2"
TABLE_SQL = "select a.*,b.column_name,b.seq_no,b.column_type,b.column_comment,b.column_business_comment from\n(\n    select table_id,table_name,database_name,create_time,modify_time,\n    comment,output_task_id,output_task_name,use_direction\n    from mysql.data_tool_box_emr.metadata_table where catalog_type='HIVE' and status='在用'\n) a\n left join mysql.data_tool_box_emr.metadata_column b on a.table_id=b.table_id"


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
        },
        "additionalProperties": False,
    }

    @staticmethod
    def _get(client, base_url, token, endpoint, params=None):
        try:
            response = client.get(
                base_url.rstrip("/") + endpoint,
                params=params,
                headers={
                    "token": token,
                    "x-requested-with": "XMLHttpRequest",
                    "cookie": f"satoken={token}",
                },
            )
            if response.status_code == 401:
                raise ValueError("DTB authentication failed (401)")
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("status")) == "401":
                raise ValueError("DTB authentication failed (401)")
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
                total = data.get("totalElements", data.get("total"))
                if not isinstance(batch, list) or isinstance(total, bool):
                    raise ValueError("Invalid DTB pagination")  # noqa: TRY004
                total = int(total)
                if total < 0 or (expected is not None and total != expected):
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
                if len(rows) == total:
                    return rows
                if not batch or len(rows) > total:
                    raise ValueError("Incomplete DTB pagination")
                page += 1
            except (KeyError, TypeError) as exc:
                raise ValueError("Invalid DTB pagination") from exc

    def list_tables(self, client, base_url, token, layer):
        return self._pages(
            client,
            base_url,
            token,
            "/api/data-map/table",
            "tableList",
            "tableId",
            {"layer": layer},
            500,
        )

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
    ):
        if format not in {"json", "xlsx", "csv"}:
            raise ValueError("Invalid DTB query format")
        headers = {
            "token": token,
            "x-requested-with": "XMLHttpRequest",
            "cookie": f"satoken={token}",
        }
        try:
            response = client.post(
                base_url.rstrip("/") + "/api/data-query/async-execute",
                headers=headers,
                json={
                    "env": 2,
                    "dataSource": data_source,
                    "sql": sql,
                    "defaultLimit": 0,
                },
            )
            if response.status_code == 401:
                raise ValueError("DTB authentication failed (401)")
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("status")) == "401":
                raise ValueError("DTB authentication failed (401)")
            if str(payload.get("status")) != "200":
                raise ValueError("DTB query submission failed")
            data = payload["data"]
            key = data.get("queryKey") or data.get("queryId")
            if not isinstance(key, str) or not key:
                raise ValueError("DTB query missing key")
            key = quote(key, safe="")
            for attempt in range(max_polls):
                result = self._get(
                    client, base_url, token, "/api/data-query/result/" + key
                )
                if result.get("errorMessage"):
                    raise ValueError("DTB query failed")
                if result.get("progress") == 1.0:
                    break
                if attempt + 1 < max_polls:
                    time.sleep(poll_interval)
            else:
                raise ValueError("DTB query timed out")
            response = client.get(
                base_url.rstrip("/") + "/api/data-query/download/" + key,
                headers=headers,
                params={"format": format},
            )
            response.raise_for_status()
            return response.content
        except httpx.TimeoutException as exc:
            raise ValueError("DTB query timeout") from exc
        except (httpx.HTTPError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("DTB query request failed") from exc

    def fetch(self, client, base_url, token):
        from .dtb_source import DtbAdapter

        tables = self.list_tables(client, base_url, token, "dimensions")
        files = {
            "任务信息.json": self.query_download(
                client, base_url, token, TASK_SQL, "json"
            ),
            "表信息.xlsx": self.query_download(
                client, base_url, token, TABLE_SQL, "xlsx"
            ),
        }
        for table in tables:
            if str(table.get("catalogType", "")).upper() != "HIVE":
                continue
            name = str(table.get("tableName", "")).removeprefix("dimensions.")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Invalid DTB dimension table name")
            _detail, columns = self.table_detail(
                client, base_url, token, table["tableId"]
            )
            content = self.query_download(
                client,
                base_url,
                token,
                f"SELECT * FROM hive.dimensions.{name} LIMIT 50000",
                "csv",
                data_source="doris_query",
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
                "truncated": len(rows) >= 50000,
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
