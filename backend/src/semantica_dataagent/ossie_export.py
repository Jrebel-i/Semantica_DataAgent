"""Deterministic export of a warehouse project's current generation to Apache Ossie."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

import yaml

from . import knowledge, project_context
from .ossie_source import (
    OSSIE_TARGET_VERSION,
    SEMANTICA_VENDOR_NAME,
    extract_single_column,
    validate_ossie_schema,
)

ExportFormat = Literal["yaml", "json"]

_SIMPLE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_TYPE_ALIASES: dict[str, str] = {
    "string": "String",
    "varchar": "String",
    "char": "String",
    "nvarchar": "String",
    "nchar": "String",
    "text": "String",
    "clob": "String",
    "int": "Integer",
    "integer": "Integer",
    "bigint": "Integer",
    "smallint": "Integer",
    "tinyint": "Integer",
    "int2": "Integer",
    "int4": "Integer",
    "int8": "Integer",
    "long": "Integer",
    "short": "Integer",
    "byte": "Integer",
    "largeint": "Integer",
    "decimal": "Decimal",
    "numeric": "Decimal",
    "number": "Decimal",
    "float": "Float",
    "double": "Float",
    "real": "Float",
    "float4": "Float",
    "float8": "Float",
    "binary_float": "Float",
    "binary_double": "Float",
    "boolean": "Boolean",
    "bool": "Boolean",
    "date": "Date",
    "time": "Time",
    "time without time zone": "Time",
    "datetime": "DateTime",
    "timestamp": "DateTime",
    "timestamp_ntz": "DateTime",
    "timestamp without time zone": "DateTime",
    "datetimetz": "DateTimeTz",
    "timestamptz": "DateTimeTz",
    "timestamp_ltz": "DateTimeTz",
    "timestamp with time zone": "DateTimeTz",
    "timestamp with local time zone": "DateTimeTz",
}


class OssieExportError(ValueError):
    """Base error raised when a project cannot be exported as Ossie."""


class UnsupportedProjectTypeError(OssieExportError):
    """Raised when attempting to export a non-warehouse project."""


class EmptyKnowledgeGenerationError(OssieExportError):
    """Raised when the current knowledge generation has no matching physical tables."""


def _table_key(table: dict[str, Any]) -> tuple[str, str, str]:
    catalog = str(table.get("catalog") or "").strip()
    return (catalog, str(table["database"]), str(table["name"]))


def _source_parts(table_key: tuple[str, str, str]) -> tuple[str, ...]:
    catalog, database, name = table_key
    return (catalog, database, name) if catalog else (database, name)


def _map_ossie_datatype(raw_type: str) -> str:
    normalized = re.sub(r"\s+", " ", raw_type.strip().lower())
    without_params = re.sub(r"\s*\([^)]*\)", "", normalized).strip()
    if without_params in _TYPE_ALIASES:
        return _TYPE_ALIASES[without_params]
    for suffix in (" unsigned", " signed"):
        if without_params.endswith(suffix):
            trimmed = without_params[: -len(suffix)].strip()
            if trimmed in _TYPE_ALIASES:
                return _TYPE_ALIASES[trimmed]
    return "Opaque"


def _column_expression(
    col_name: str, dataset_name: str, source_parts: tuple[str, ...]
) -> dict[str, str]:
    if (
        _SIMPLE_IDENTIFIER_RE.fullmatch(col_name)
        and extract_single_column(
            col_name, "OSSIE_SQL_2026", dataset_name, source_parts
        )
        == col_name
    ):
        return {"sql": col_name}
    quoted = '"' + col_name.replace('"', '""') + '"'
    return {"sql": quoted}


def _assign_dataset_names(
    sorted_keys: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], str]:
    short_counts = Counter(name for _, _, name in sorted_keys)
    db_name_counts = Counter((database, name) for _, database, name in sorted_keys)

    candidates: list[tuple[tuple[str, str, str], str]] = []
    for key in sorted_keys:
        catalog, database, name = key
        if short_counts[name] == 1:
            base = name
        elif db_name_counts[(database, name)] == 1 or not catalog:
            base = f"{database}__{name}"
        else:
            base = f"{catalog}__{database}__{name}"
        candidates.append((key, base))

    candidate_counts = Counter(base for _, base in candidates)
    used: set[str] = set()
    assigned: dict[tuple[str, str, str], str] = {}
    for key, base in candidates:
        catalog, database, name = key
        chosen = base
        if candidate_counts[chosen] > 1 or chosen in used:
            escalated = (
                f"{catalog}__{database}__{name}" if catalog else f"{database}__{name}"
            )
            if (
                escalated != chosen
                and candidate_counts[escalated] == 0
                and escalated not in used
            ):
                chosen = escalated
            else:
                suffix = 2
                while f"{chosen}__{suffix}" in used:
                    suffix += 1
                chosen = f"{chosen}__{suffix}"
        used.add(chosen)
        assigned[key] = chosen
    return assigned


def _document_title(doc: dict[str, Any]) -> str:
    content = str(doc.get("content") or "")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            if heading:
                return heading
    return str(doc.get("path") or doc.get("ref") or "")


def _build_ai_context(docs: list[dict[str, Any]]) -> dict[str, str] | None:
    if not docs:
        return None
    unique_by_ref: dict[str, dict[str, Any]] = {}
    for doc in docs:
        ref = str(doc.get("ref") or "").strip()
        if not ref or doc.get("is_index") is True or doc.get("path") == "index.md":
            continue
        unique_by_ref.setdefault(ref, doc)
    if not unique_by_ref:
        return None

    lines = ["关联知识文档："]
    for ref in sorted(unique_by_ref):
        doc = unique_by_ref[ref]
        title = _document_title(doc)
        category = str(doc.get("category") or "").strip()
        path = str(doc.get("path") or "").strip()
        rel_path = f"{category}/{path}" if category and path else path
        if rel_path and title != path:
            lines.append(f"- {title}（{rel_path}，ref: {ref}）")
        elif rel_path:
            lines.append(f"- {rel_path}（ref: {ref}）")
        else:
            lines.append(f"- {title}（ref: {ref}）")
    return {"instructions": "\n".join(lines)}


def _build_field(
    field: dict[str, Any], dataset_name: str, source_parts: tuple[str, ...]
) -> dict[str, Any]:
    col_name = str(field["name"])
    raw_type = str(field.get("type") or "").strip() or "string"
    entry: dict[str, Any] = {
        "name": col_name,
        "expression": _column_expression(col_name, dataset_name, source_parts),
        "datatype": _map_ossie_datatype(raw_type),
        "nonportable_extensions": [
            {
                "vendor_name": SEMANTICA_VENDOR_NAME,
                "data": {
                    "partition": bool(field.get("partition", False)),
                    "raw_type": raw_type,
                },
            }
        ],
    }
    comment = str(field.get("comment") or "").strip()
    if comment:
        entry["description"] = comment
    return entry


def _build_relationships(
    rel_rows: list[dict[str, Any]],
    dataset_names: dict[tuple[str, str, str], str],
    ref_to_key: dict[str, tuple[str, str, str]],
) -> list[dict[str, Any]]:
    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for row in rel_rows:
        props = row.get("relationship") or {}
        from_key = ref_to_key.get(row.get("from_ref") or "") or (
            str(row.get("from_catalog") or "").strip(),
            str(row.get("from_database") or ""),
            str(row.get("from_name") or ""),
        )
        to_key = ref_to_key.get(row.get("to_ref") or "") or (
            str(row.get("to_catalog") or "").strip(),
            str(row.get("to_database") or ""),
            str(row.get("to_name") or ""),
        )
        if from_key not in dataset_names or to_key not in dataset_names:
            continue
        from_cols = [str(c) for c in (props.get("from_columns") or ())]
        to_cols = [str(c) for c in (props.get("to_columns") or ())]
        if not from_cols or len(from_cols) != len(to_cols):
            continue
        raw_name = str(props.get("name") or "").strip()
        if not raw_name:
            continue
        from_ds = dataset_names[from_key]
        to_ds = dataset_names[to_key]
        sort_key = (
            from_key,
            to_key,
            raw_name,
            tuple(from_cols),
            tuple(to_cols),
        )
        candidates.append(
            (
                sort_key,
                {
                    "name": raw_name,
                    "from": from_ds,
                    "to": to_ds,
                    "from_columns": from_cols,
                    "to_columns": to_cols,
                },
            )
        )

    candidates.sort(key=lambda item: item[0])
    name_counts = Counter(item[1]["name"] for item in candidates)
    used_names: set[str] = set()
    relationships: list[dict[str, Any]] = []
    for _, rel in candidates:
        name = rel["name"]
        if name_counts[name] > 1 or name in used_names:
            candidate = f"{rel['from']}__{rel['to']}__{name}"
            if candidate in used_names or name_counts[candidate] > 0:
                suffix = 2
                while f"{candidate}__{suffix}" in used_names:
                    suffix += 1
                candidate = f"{candidate}__{suffix}"
            rel = {**rel, "name": candidate}
        used_names.add(rel["name"])
        relationships.append(rel)
    return relationships


def validate_ossie_document(document: dict[str, Any]) -> None:
    """Validate an exported Ossie document against the bundled 0.3.0.dev0 JSON Schema."""
    validate_ossie_schema(
        document,
        context="Exported Ossie document failed schema validation",
        error_cls=OssieExportError,
    )


def _query_graph_snapshot(
    project_id: str, database: str | None
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    values = (
        knowledge.settings()
        if project_id in (None, project_context.DEFAULT_PROJECT_ID)
        else knowledge.settings(project_id)
    )
    project = project_context.from_values(values, project_id)
    if (
        project.project_id != project_context.DEFAULT_PROJECT_ID
        and not project_context.generation_is_built(
            project.project_id, project.generation
        )
    ):
        raise EmptyKnowledgeGenerationError(
            f"项目 {project_id!r} 当前知识代尚无已构建的物理表"
        )
    try:
        store = knowledge.read_graph(values)
    except KeyError as exc:
        if exc.args == ("neo4j_uri",) and not values.get("neo4j_uri"):
            raise EmptyKnowledgeGenerationError(
                f"项目 {project_id!r} 当前知识代尚无已构建的物理表"
            ) from exc
        raise

    params: dict[str, Any] = {
        "generation": project.generation,
        "database": database,
    }
    table_rows = knowledge.execute(
        store,
        """MATCH (t:SDATable {generation:$generation})
        WHERE ($database IS NULL OR t.database = $database)
        OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField {generation:$generation})
        WITH t, collect(properties(f)) AS fields
        RETURN properties(t) AS table, fields
        ORDER BY coalesce(table.catalog, ''), table.database, table.name""",
        params,
    )
    if not table_rows:
        scope_note = f"（database={database!r}）" if database is not None else ""
        raise EmptyKnowledgeGenerationError(
            f"项目 {project_id!r} 当前知识代没有可导出的物理表{scope_note}"
        )

    doc_rows = knowledge.execute(
        store,
        """MATCH (t:SDATable {generation:$generation})-[:HAS_DOCUMENT]->(d:SDADocument {generation:$generation})
        WHERE ($database IS NULL OR t.database = $database)
          AND coalesce(d.is_index, false) = false
          AND d.path <> 'index.md'
        RETURN t.ref AS table_ref, properties(d) AS document
        ORDER BY table_ref, document.ref""",
        params,
    )
    docs_by_table_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in doc_rows:
        table_ref = row.get("table_ref")
        doc = row.get("document")
        if table_ref and isinstance(doc, dict):
            docs_by_table_ref[str(table_ref)].append(doc)

    rel_rows = knowledge.execute(
        store,
        """MATCH (a:SDATable {generation:$generation})-[r:RELATES_TO]->(b:SDATable {generation:$generation})
        WHERE $database IS NULL OR (a.database = $database AND b.database = $database)
        RETURN a.ref AS from_ref,
               coalesce(a.catalog, '') AS from_catalog,
               a.database AS from_database,
               a.name AS from_name,
               b.ref AS to_ref,
               coalesce(b.catalog, '') AS to_catalog,
               b.database AS to_database,
               b.name AS to_name,
               properties(r) AS relationship""",
        params,
    )
    return table_rows, docs_by_table_ref, rel_rows


def export_ossie_document(
    project_id: str = project_context.DEFAULT_PROJECT_ID,
    *,
    database: str | None = None,
    project_type: str | None = None,
) -> dict[str, Any]:
    """Build and schema-validate an Apache Ossie 0.3.0.dev0 dict from the active generation."""
    if not project_id or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")

    resolved_type = (
        project_type
        if project_type is not None
        else project_context.project_type(project_id)
    )
    if resolved_type != "warehouse":
        raise UnsupportedProjectTypeError(
            f"仅数仓型项目支持导出为 Apache Ossie（当前项目类型：{resolved_type}）"
        )

    table_rows, docs_by_table_ref, rel_rows = _query_graph_snapshot(
        project_id, database
    )

    sorted_rows = sorted(table_rows, key=lambda row: _table_key(row["table"]))
    sorted_keys = [_table_key(row["table"]) for row in sorted_rows]
    dataset_names = _assign_dataset_names(sorted_keys)

    ref_to_key: dict[str, tuple[str, str, str]] = {}
    datasets: list[dict[str, Any]] = []
    for row in sorted_rows:
        table = row["table"]
        key = _table_key(table)
        if table.get("ref"):
            ref_to_key[str(table["ref"])] = key
        ds_name = dataset_names[key]
        parts = _source_parts(key)
        raw_fields = [
            f
            for f in (row.get("fields") or ())
            if isinstance(f, dict) and f.get("name")
        ]
        raw_fields.sort(key=lambda item: str(item["name"]))
        fields = [_build_field(field, ds_name, parts) for field in raw_fields]

        dataset: dict[str, Any] = {
            "name": ds_name,
            "source": ".".join(parts),
        }
        comment = str(table.get("comment") or "").strip()
        if comment:
            dataset["description"] = comment
        ai_context = _build_ai_context(
            docs_by_table_ref.get(str(table.get("ref") or ""), [])
        )
        if ai_context is not None:
            dataset["ai_context"] = ai_context
        if fields:
            dataset["fields"] = fields
        datasets.append(dataset)

    document: dict[str, Any] = {
        "version": OSSIE_TARGET_VERSION,
        "name": project_id,
        "datasets": datasets,
    }
    relationships = _build_relationships(rel_rows, dataset_names, ref_to_key)
    if relationships:
        document["relationships"] = relationships

    validate_ossie_document(document)
    return document


def serialize_ossie(document: dict[str, Any], fmt: ExportFormat = "yaml") -> str:
    """Serialize a validated Ossie document as deterministic YAML or JSON."""
    validate_ossie_document(document)
    if fmt == "yaml":
        return yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    if fmt == "json":
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    raise ValueError(f"Unsupported Ossie export format: {fmt!r}")


def export_ossie(
    project_id: str = project_context.DEFAULT_PROJECT_ID,
    *,
    format: ExportFormat = "yaml",
    database: str | None = None,
    project_type: str | None = None,
) -> str:
    """Export a warehouse project's current generation to a validated Ossie string."""
    if format not in ("yaml", "json"):
        raise ValueError(f"Unsupported Ossie export format: {format!r}")
    document = export_ossie_document(
        project_id=project_id,
        database=database,
        project_type=project_type,
    )
    return serialize_ossie(document, format)


def write_ossie_file(output: Path, content: str) -> None:
    """Atomically write exported Ossie content via a same-directory temp file and os.replace."""
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
