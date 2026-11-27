"""DTB export snapshot reader: task JSON, table-info workbook and dimension rows."""

import hashlib
import io
import json
import re
import warnings
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

from .canonical import ReadResult, Record

TABLE_COLUMNS = (
    "table_id",
    "table_name",
    "database_name",
    "comment",
    "output_task_id",
    "output_task_name",
    "column_name",
    "seq_no",
    "column_type",
    "column_comment",
    "column_business_comment",
)
TYPE_ALIASES = {
    "spark sql": "spark",
    "spark_sql": "spark",
    "spark": "spark",
    "shell": "shell",
    "merge onself": "merge_onself",
    "merge_onself": "merge_onself",
}
PARTITION_SEQ_START = 10001


def _text(value):
    if value is None:
        return ""
    return str(value)


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _task_type(value):
    key = re.sub(r"\s+", " ", value.strip().lower())
    return TYPE_ALIASES.get(key, _slug(key))


def _decode_task_conf(raw, stats):
    """Decode task_conf, including strings that were JSON-encoded twice."""
    text = raw.strip()
    if not text:
        stats["task_conf_empty"] += 1
        return None
    value = text
    for _ in range(4):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            stats["task_conf_unreadable"] += 1
            return None
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            stats["task_conf_double_encoded"] += 1
            value = parsed.strip()
            if not value:
                return None
            continue
        stats["task_conf_not_object"] += 1
        return None
    stats["task_conf_too_deep"] += 1
    return None


def _upstream_ids(raw):
    return [part for part in re.split(r"[\s,;]+", raw) if part]


def _qualified(value):
    text = _text(value).strip().strip(";").replace("`", "").replace('"', "")
    parts = [part.strip() for part in text.split(".") if part.strip()]
    if len(parts) < 2 or parts[-2].lower() in {"default", "<default>"}:
        return None
    return f"{parts[-2]}.{parts[-1]}"


class DtbSnapshotReader:
    """Read DTB snapshot directories (任务信息.json, 表信息.xlsx, dimensions) to canonical records."""

    def read(self, directory, parameters=None, *, include_dimensions=True):
        parameters = parameters or {}
        base = Path(directory)
        if not base.is_dir():
            raise ValueError(f"Source directory does not exist: {base}")
        stats = Counter()
        snapshot = self._snapshot(base, parameters, stats)
        hashes = {}
        records = []

        manifest = snapshot / "manifest.json"
        if manifest.is_file():
            self._hash(manifest, base, hashes)
        tasks, task_ids, task_outputs, upstream = self._tasks(
            snapshot / "任务信息.json", base, hashes, stats
        )
        tables, tables_by_task = self._tables(
            snapshot / "表信息.xlsx", base, hashes, stats
        )
        declared = self._declared_lineage(
            tasks,
            task_ids,
            task_outputs,
            upstream,
            tables_by_task,
            stats,
            (snapshot / "任务信息.json").relative_to(base).as_posix(),
            (snapshot / "表信息.xlsx").relative_to(base).as_posix(),
        )
        dimensions_parameters = dict(parameters)
        if (snapshot / "dimensions").is_dir():
            dimensions_parameters["dimensions_path"] = (
                (snapshot / "dimensions").relative_to(base).as_posix()
            )
        dimensions = (
            self._dimensions(base, dimensions_parameters, hashes, stats)
            if include_dimensions
            else []
        )

        records.extend(tasks)
        records.extend(tables)
        records.extend(declared)
        records.extend(dimensions)
        stats["files_hashed"] = len(hashes)
        return ReadResult(records, hashes, dict(stats))

    def _snapshot(self, base, parameters, stats):
        relative = Path(parameters.get("snapshot_path", "dtb"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("snapshot_path must be relative to the source directory")
        root = base / relative
        if not root.is_dir():
            raise ValueError(f"DTB snapshot directory does not exist: {root}")
        explicit = parameters.get("snapshot")
        if explicit:
            name = Path(explicit)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("snapshot must be relative to snapshot_path")
            snapshot = root / name
            if not (snapshot / "任务信息.json").is_file():
                raise ValueError(f"DTB snapshot has no task file: {snapshot}")
            return snapshot
        if not parameters.get("latest", True):
            raise ValueError("snapshot is required when latest is false")
        candidates = sorted(
            path
            for path in root.iterdir()
            if path.is_dir() and (path / "任务信息.json").is_file()
        )
        if candidates:
            stats["snapshot_directories"] = len(candidates)
        marked = []
        for candidate in candidates:
            manifest = candidate / "pull-manifest.json"
            if manifest.exists():
                from datetime import datetime

                try:
                    published = datetime.fromisoformat(
                        json.loads(manifest.read_text(encoding="utf-8"))["published_at"]
                    )
                    if published.utcoffset() is None:
                        raise ValueError("Missing timezone")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise ValueError("Invalid pull snapshot manifest") from exc
                marked.append((published, candidate.name, candidate))
        if marked:
            return max(marked)[2]
        if (root / "任务信息.json").is_file():
            return root
        if candidates:
            return candidates[-1]
        raise ValueError(f"No DTB snapshot found under {root}")

    @staticmethod
    def _hash(path, base, hashes):
        raw = path.read_bytes()
        hashes[path.relative_to(base).as_posix()] = hashlib.sha256(raw).hexdigest()
        return raw

    def _tasks(self, path, base, hashes, stats):
        if not path.is_file():
            raise ValueError(f"DTB task file does not exist: {path}")
        raw = self._hash(path, base, hashes)
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid DTB task file {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(  # noqa: TRY004 - file contents, not an argument type
                f"Invalid DTB task file {path}: expected an object"
            )
        columns, rows = payload.get("columns"), payload.get("data")
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValueError(  # noqa: TRY004 - file contents, not an argument type
                f"Invalid DTB task file {path}: columns and data required"
            )
        names = []
        for index, column in enumerate(columns):
            name = _text(column.get("name") if isinstance(column, dict) else "").strip()
            if not name:
                raise ValueError(f"Invalid DTB task column at index {index}")
            names.append(name)
        missing = [name for name in ("task_name", "task_src") if name not in names]
        if missing:
            raise ValueError(f"DTB task file lacks columns: {', '.join(missing)}")

        source = path.relative_to(base).as_posix()
        records, task_ids, outputs, upstream = [], {}, {}, {}
        for row in rows:
            if not isinstance(row, list):
                stats["task_rows_unreadable"] += 1
                continue
            if len(row) < len(names):
                stats["task_rows_truncated"] += 1
            elif len(row) > len(names):
                stats["task_rows_extra_values"] += 1
            values = list(row) + [None] * (len(names) - len(row))
            fields = dict(zip(names, values, strict=False))
            name = _text(fields.get("task_name")).strip()
            if not name:
                stats["tasks_without_name"] += 1
                continue
            task_id = _text(fields.get("task_id")).strip()
            if not task_id:
                stats["tasks_without_id"] += 1
            task_type = _text(fields.get("task_type")).strip()
            if not task_type:
                stats["tasks_without_type"] += 1
            sql = _text(fields.get("task_src"))
            if not sql.strip():
                stats["tasks_without_sql"] += 1
            parameters = _decode_task_conf(_text(fields.get("task_conf")), stats)
            data = {"name": name, "sql": sql, "type": _task_type(task_type)}
            description = _text(fields.get("project_name")).strip()
            if description:
                data["description"] = description
            if parameters is not None:
                data["parameters"] = parameters
            records.append(Record("task", data, source))
            if task_id:
                task_ids[task_id] = name
            output = _qualified(fields.get("output_table_name"))
            if output:
                outputs[name] = [output]
            upstream[name] = _upstream_ids(_text(fields.get("upstream_tasks")))
        return records, task_ids, outputs, upstream

    def _tables(self, path, base, hashes, stats):
        if not path.is_file():
            raise ValueError(f"DTB table info file does not exist: {path}")
        raw = self._hash(path, base, hashes)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Workbook contains no default style",
                category=UserWarning,
            )
            book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        try:
            rows = book.active.values
            try:
                header = next(rows)
            except StopIteration:
                raise ValueError(f"DTB table info file is empty: {path}") from None
            headers = [_text(value).strip() for value in header]
            missing = [
                name
                for name in ("database_name", "table_name", "column_name")
                if name not in headers
            ]
            if missing:
                raise ValueError(f"DTB table info lacks columns: {', '.join(missing)}")
            stats["table_columns_missing"] = len(
                [name for name in TABLE_COLUMNS if name not in headers]
            )
            indexes = {}
            for index, name in enumerate(headers):
                indexes.setdefault(name, index)

            def cell(values, name):
                index = indexes.get(name)
                if index is None or index >= len(values):
                    return None
                return values[index]

            table_rows = {}
            tables_by_task = {}
            for row in rows:
                values = list(row)
                if not any(_text(value).strip() for value in values):
                    continue
                database = _text(cell(values, "database_name")).strip()
                table = _text(cell(values, "table_name")).strip()
                column = _text(cell(values, "column_name")).strip()
                if not database or not table or not column:
                    stats["table_rows_skipped"] += 1
                    continue
                seq_text = _text(cell(values, "seq_no")).strip()
                try:
                    seq_no = int(float(seq_text)) if seq_text else None
                except ValueError:
                    stats["invalid_seq_no"] += 1
                    seq_no = None
                comment = _text(cell(values, "column_comment")).strip()
                if not comment:
                    comment = _text(cell(values, "column_business_comment")).strip()
                field_type = _text(cell(values, "column_type")).strip()
                if not field_type:
                    stats["fields_without_type"] += 1
                    field_type = "unknown"
                key = (database, table)
                if key not in table_rows:
                    table_rows[key] = {
                        "database": database,
                        "name": table,
                        "comment": _text(cell(values, "comment")).strip(),
                        "fields": [],
                    }
                table_rows[key]["fields"].append(
                    {
                        "database": database,
                        "table": table,
                        "name": column,
                        "type": field_type,
                        "comment": comment,
                        "partition": seq_no is not None
                        and seq_no >= PARTITION_SEQ_START,
                    }
                )
                output_task_id = _text(cell(values, "output_task_id")).strip()
                if output_task_id:
                    tables_by_task.setdefault(output_task_id, set()).add(
                        f"{database}.{table}"
                    )

            source = path.relative_to(base).as_posix()
            records = []
            for key in sorted(table_rows):
                item = table_rows[key]
                records.append(
                    Record(
                        "table",
                        {
                            "database": item["database"],
                            "name": item["name"],
                            "comment": item["comment"],
                        },
                        source,
                    )
                )
                records.extend(
                    Record("field", field, source) for field in item["fields"]
                )
            return records, {
                task_id: sorted(names) for task_id, names in tables_by_task.items()
            }
        finally:
            book.close()

    def _declared_lineage(
        self,
        tasks,
        task_ids,
        task_outputs,
        upstream,
        tables_by_task,
        stats,
        task_source,
        table_source,
    ):
        name_by_id = task_ids
        outputs = {name: list(names) for name, names in task_outputs.items()}
        for task_id, names in tables_by_task.items():
            name = name_by_id.get(task_id)
            if name:
                outputs[name] = list(names)

        records, seen = [], set()
        for task in tasks:
            name = task.data["name"]
            downstreams = outputs.get(name, [])
            if not downstreams:
                stats["tasks_without_output"] += 1
                continue
            for source_id in upstream.get(name, []):
                upstream_name = name_by_id.get(source_id)
                upstream_tables = tables_by_task.get(source_id, [])
                if not upstream_tables and upstream_name:
                    upstream_tables = outputs.get(upstream_name, [])
                if not upstream_tables:
                    stats["upstream_refs_unresolved"] += 1
                    continue
                for source in upstream_tables:
                    for downstream in downstreams:
                        if source == downstream:
                            continue
                        key = (source, downstream, name)
                        if key in seen:
                            continue
                        seen.add(key)
                        records.append(
                            Record(
                                "lineage",
                                {
                                    "upstream": source,
                                    "downstream": downstream,
                                    "evidence": "declared",
                                    "parse_status": "success",
                                    "task_id": name,
                                },
                                task_source,
                                None,
                                (task_source, table_source),
                            )
                        )
        return records

    def iter_dimension_tables(
        self,
        directory,
        parameters=None,
        *,
        stats=None,
        hashes=None,
        include_catalog=False,
    ):
        """Yield (database, table, rows_source, meta_source, rows_iter) per dimension table."""
        parameters = parameters or {}
        base = Path(directory)
        stats = stats if stats is not None else Counter()
        hashes = hashes if hashes is not None else {}
        relative = Path(parameters.get("dimensions_path", "dimensions"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("dimensions_path must be relative to the source directory")
        root = base / relative
        limit = int(parameters.get("dimension_row_limit", 50000))
        wanted = parameters.get("dimension_tables")
        seen = set()
        if not root.is_dir():
            stats["dimensions_directory_missing"] += 1
            return
        for table_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            if wanted is not None and table_dir.name not in wanted:
                continue
            seen.add(table_dir.name)
            stats["dimension_tables"] += 1
            rows_path = table_dir / "current" / "rows.jsonl"
            meta_path = table_dir / "meta.json"
            if not meta_path.is_file():
                stats["dimension_tables_without_meta"] += 1
                continue
            if not rows_path.is_file():
                stats["dimension_tables_without_rows"] += 1
                continue
            try:
                meta = json.loads(
                    self._hash(meta_path, base, hashes).decode("utf-8-sig")
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                stats["dimension_tables_unreadable"] += 1
                continue
            if not isinstance(meta, dict):
                stats["dimension_tables_unreadable"] += 1
                continue
            database = _text(meta.get("database")).strip()
            catalog = _text(meta.get("catalog")).strip()
            qualified = _text(meta.get("qualified_table")).strip()
            qualified_parts = qualified.split(".")
            if len(qualified_parts) == 3:
                catalog = catalog or qualified_parts[0]
            table = _text(meta.get("table")).strip() or qualified
            if "." in table:
                parts = table.split(".")
                if len(parts) == 3:
                    qualified_catalog, database, table = parts
                    catalog = catalog or qualified_catalog
                else:
                    database, table = table.rsplit(".", 1)
            elif not database and "." in qualified:
                parts = qualified.split(".")
                if len(parts) == 3:
                    qualified_catalog, database, _ = parts
                    catalog = catalog or qualified_catalog
                else:
                    database = qualified.rsplit(".", 1)[0]
            table = table or table_dir.name
            if not database:
                stats["dimension_tables_without_database"] += 1
                continue
            if meta.get("truncated"):
                stats["dimension_truncated_tables"] += 1
            columns = {
                _text(column.get("name")).strip()
                for column in meta.get("columns") or []
                if isinstance(column, dict)
            }
            meta_source = meta_path.relative_to(base).as_posix()
            rows_source = rows_path.relative_to(base).as_posix()

            def _row_stream(
                _rows_path=rows_path,
                _base=base,
                _hashes=hashes,
                _stats=stats,
                _columns=columns,
                _limit=limit,
                _rows_source=rows_source,
            ):
                seen_columns = set()
                rows_read = 0
                digest = hashlib.sha256()
                with _rows_path.open("rb") as fh:
                    for line_number, raw_line in enumerate(fh, start=1):
                        digest.update(raw_line)
                        if not raw_line.strip():
                            continue
                        if rows_read >= _limit:
                            _stats["dimension_row_limited_tables"] += 1
                            break
                        try:
                            row = json.loads(raw_line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            _stats["dimension_rows_unreadable"] += 1
                            continue
                        if not isinstance(row, dict):
                            _stats["dimension_rows_unreadable"] += 1
                            continue
                        rows_read += 1
                        cells = []
                        for field, value in row.items():
                            if not isinstance(field, str) or not field.strip():
                                _stats["dimension_fields_unnamed"] += 1
                                continue
                            if value is None or not isinstance(value, str):
                                value = _text(value)
                            if not value.strip():
                                _stats["dimension_empty_cells"] += 1
                                continue
                            seen_columns.add(field)
                            cells.append((field, value))
                        yield line_number, cells
                    # Hash any remaining bytes if limit broke early
                    for chunk in iter(lambda: fh.read(65536), b""):
                        digest.update(chunk)
                _hashes[_rows_source] = digest.hexdigest()
                _stats["dimension_rows"] += rows_read
                for column in _columns - seen_columns:
                    _stats["dimension_columns_without_rows"] += 1

            result = (database, table, rows_source, meta_source, _row_stream())
            yield (catalog, *result) if include_catalog else result

        if wanted is not None:
            stats["dimension_tables_requested_missing"] += len(set(wanted) - seen)

    def _dimensions(self, base, parameters, hashes, stats):
        records = []
        for (
            catalog,
            database,
            table,
            rows_source,
            meta_source,
            row_stream,
        ) in self.iter_dimension_tables(
            base, parameters, stats=stats, hashes=hashes, include_catalog=True
        ):
            for line_number, cells in row_stream:
                row_id = f"{catalog + '.' if catalog else ''}{database}.{table}:{line_number}"
                for field, value in cells:
                    records.append(
                        Record(
                            "dimension_value",
                            {
                                **({"catalog": catalog} if catalog else {}),
                                "database": database,
                                "table": table,
                                "field": field,
                                "value": value,
                                "row_id": row_id,
                            },
                            rows_source,
                            None,
                            (rows_source, meta_source),
                        )
                    )
        return records
