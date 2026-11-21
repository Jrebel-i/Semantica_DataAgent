"""Apache Ossie semantic model source adapter and derived document builder."""

from __future__ import annotations

import collections.abc
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, ClassVar

import sqlglot
import yaml
from jsonschema import Draft202012Validator
from sqlglot import exp
from sqlglot.errors import ParseError

from .canonical import ReadResult, Record
from .source_directory import category_path, safe, safe_name

OSSIE_TARGET_VERSION = "0.3.0.dev0"
SEMANTICA_VENDOR_NAME = "SEMANTICA_DATAAGENT"
MODEL_EXTENSIONS = {".yaml", ".yml", ".json"}

DATATYPE_MAP: dict[str, str] = {
    "String": "string",
    "Integer": "integer",
    "Decimal": "decimal",
    "Float": "float",
    "Boolean": "boolean",
    "Date": "date",
    "Time": "time",
    "DateTime": "timestamp",
    "DateTimeTz": "timestamptz",
    "Opaque": "opaque",
}

SQLGLOT_DIALECT_MAP: dict[str, str | None] = {
    "OSSIE_SQL_2026": None,
    "ANSI_SQL": None,
    "SPARK": "spark",
    "SPARK_SQL": "spark",
    "HIVE": "hive",
    "DORIS": "doris",
    "MYSQL": "mysql",
    "POSTGRES": "postgres",
    "POSTGRESQL": "postgres",
    "BIGQUERY": "bigquery",
    "SNOWFLAKE": "snowflake",
    "TRINO": "trino",
    "PRESTO": "trino",
    "DUCKDB": "duckdb",
}


class _UniqueKeyYamlLoader(yaml.SafeLoader):
    """YAML SafeLoader that rejects duplicate explicit mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, collections.abc.Hashable):
                    raise ValueError(  # noqa: TRY004 - file contents, not an argument type
                        f"Unhashable YAML key: {key!r}"
                    )
                if key in seen:
                    raise ValueError(f"Duplicate YAML key: {key!r}")
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


@cache
def _ossie_validator() -> Draft202012Validator:
    packaged = (
        Path(__file__).resolve().parent / "schemas" / "ossie" / "ossie-schema.json"
    )
    schema_path = (
        packaged
        if packaged.is_file()
        else Path(__file__).resolve().parents[3]
        / "schemas"
        / "ossie"
        / "ossie-schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def validate_ossie_schema(
    document: dict[str, Any],
    *,
    context: str = "",
    error_cls: type[Exception] = ValueError,
) -> None:
    """Validate an Ossie document against the bundled 0.3.0.dev0 JSON Schema."""
    errors = sorted(
        _ossie_validator().iter_errors(document),
        key=lambda err: [str(part) for part in err.absolute_path],
    )
    if errors:
        first = errors[0]
        loc = ".".join(str(part) for part in first.absolute_path)
        detail = f"{loc}: {first.message}" if loc else first.message
        message = f"{context}: {detail}" if context else detail
        raise error_cls(message)


def _load_raw_document(path: Path, raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Invalid UTF-8 encoding: {exc}") from exc
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
    try:
        loaded = yaml.load(text, Loader=_UniqueKeyYamlLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    if loaded is None:
        raise ValueError("Ossie model file must not be empty")
    return loaded


def _normalize_extensions(node: Any, *, legacy_version: bool) -> Any:
    if not legacy_version:
        return node
    if isinstance(node, list):
        return [
            _normalize_extensions(item, legacy_version=legacy_version) for item in node
        ]
    if not isinstance(node, dict):
        return node
    out = dict(node)
    has_custom = "custom_extensions" in out
    has_nonportable = "nonportable_extensions" in out
    if has_custom and has_nonportable:
        raise ValueError(
            "Cannot specify both custom_extensions and nonportable_extensions"
        )
    if has_custom:
        out["nonportable_extensions"] = out.pop("custom_extensions")
    extensions = out.get("nonportable_extensions")
    if isinstance(extensions, list):
        out["nonportable_extensions"] = _decode_legacy_extensions(extensions)
    for key, value in list(out.items()):
        if key == "nonportable_extensions":
            continue
        out[key] = _normalize_extensions(value, legacy_version=legacy_version)
    return out


def _decode_legacy_extensions(extensions: list[Any]) -> list[Any]:
    normalized = []
    for ext in extensions:
        if not isinstance(ext, dict):
            normalized.append(ext)
            continue
        ext_copy = dict(ext)
        raw_data = ext_copy.get("data")
        if isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON string in extension data: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(  # noqa: TRY004 - file contents, not an argument type
                    "Extension data JSON string must decode to an object"
                )
            ext_copy["data"] = parsed
        normalized.append(ext_copy)
    return normalized


def _normalize_and_validate_model(raw_obj: Any, source_rel: str) -> dict[str, Any]:
    if isinstance(raw_obj, list):
        raise ValueError(  # noqa: TRY004 - file contents, not an argument type
            f"{source_rel}: Multi-model Ossie files are not supported; split each semantic model into its own file"
        )
    if not isinstance(raw_obj, dict):
        raise ValueError(  # noqa: TRY004 - file contents, not an argument type
            f"{source_rel}: Ossie model must be a mapping object"
        )
    if "semantic_models" in raw_obj:
        raise ValueError(
            f"{source_rel}: Multi-model Ossie files are not supported; split each semantic model into its own file"
        )
    model, wrapped = _unwrap_model(raw_obj, source_rel)

    version = model.get("version")
    is_legacy_ver = isinstance(version, str) and (
        version in {"0.1", "0.2"} or version.startswith(("0.1.", "0.2."))
    )
    is_legacy = wrapped or is_legacy_ver
    if (wrapped and version is None) or is_legacy_ver:
        model["version"] = OSSIE_TARGET_VERSION

    model = _normalize_extensions(model, legacy_version=is_legacy)

    validate_ossie_schema(model, context=source_rel)
    return model


def _unwrap_model(
    raw_obj: dict[str, Any], source_rel: str
) -> tuple[dict[str, Any], bool]:
    if "semantic_model" not in raw_obj:
        return dict(raw_obj), False
    extra_outer = set(raw_obj) - {"semantic_model", "version"}
    if extra_outer:
        raise ValueError(
            f"{source_rel}: Unexpected wrapper keys alongside semantic_model: {', '.join(sorted(extra_outer))}"
        )
    inner = raw_obj["semantic_model"]
    if isinstance(inner, list):
        raise ValueError(  # noqa: TRY004 - file contents, not an argument type
            f"{source_rel}: Multi-model Ossie wrapper lists are not supported; split each semantic model into its own file"
        )
    if not isinstance(inner, dict):
        raise ValueError(  # noqa: TRY004 - file contents, not an argument type
            f"{source_rel}: semantic_model wrapper must be an object"
        )
    model = dict(inner)
    outer_version = raw_obj.get("version")
    if (
        "version" in model
        and outer_version is not None
        and model["version"] != outer_version
    ):
        raise ValueError(
            f"{source_rel}: Conflicting version between wrapper and semantic_model"
        )
    if "version" not in model and outer_version is not None:
        model["version"] = outer_version
    return model, True


def _unquote_segment(segment: str) -> str:
    text = segment.strip()
    if len(text) >= 2 and (
        (text[0] == "`" and text[-1] == "`") or (text[0] == '"' and text[-1] == '"')
    ):
        text = text[1:-1].strip()
    return text


def _parse_dataset_source(
    source: str, *, strip_catalog: bool
) -> tuple[str | None, str, str, tuple[str, ...]]:
    parts = [_unquote_segment(part) for part in source.strip().split(".")]
    if len(parts) not in (2, 3) or any(
        not part or any(ch.isspace() for ch in part) for part in parts
    ):
        raise ValueError(
            f"Invalid dataset source {source!r}: expected database.table or catalog.database.table"
        )
    if len(parts) == 2:
        return None, parts[0], parts[1], tuple(parts)
    if strip_catalog:
        return None, parts[1], parts[2], tuple(parts)
    return parts[0], parts[1], parts[2], tuple(parts)


def _select_expression(expr_obj: dict[str, Any]) -> tuple[str, str]:
    sql = expr_obj.get("sql")
    if sql:
        return sql, "OSSIE_SQL_2026"
    dialects = expr_obj.get("dialects") or []
    for preferred in ("OSSIE_SQL_2026", "ANSI_SQL"):
        for entry in dialects:
            if entry["dialect"] == preferred:
                return entry["expression"], entry["dialect"]
    for preferred in ("OSSIE_SQL_2026", "ANSI_SQL"):
        for entry in dialects:
            if entry["dialect"].upper() == preferred:
                return entry["expression"], entry["dialect"]
    return dialects[0]["expression"], dialects[0]["dialect"]


def _all_expressions(expr_obj: dict[str, Any]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if expr_obj.get("sql"):
        items.append(("OSSIE_SQL_2026", expr_obj["sql"]))
    for entry in expr_obj.get("dialects") or []:
        items.append((entry["dialect"], entry["expression"]))
    return items


def extract_single_column(
    expr_text: str,
    dialect: str,
    dataset_name: str,
    source_parts: tuple[str, ...],
) -> str | None:
    """Return the physical column name when an expression selects one column on the dataset."""
    read_dialect = SQLGLOT_DIALECT_MAP.get(dialect.upper())
    try:
        node = sqlglot.parse_one(expr_text, read=read_dialect)
    except ParseError:
        if read_dialect is not None or "`" not in expr_text:
            return None
        try:
            node = sqlglot.parse_one(expr_text, read="spark")
        except ParseError:
            return None
    if not isinstance(node, exp.Column) or not isinstance(node.this, exp.Identifier):
        return None
    parts = [part.name for part in node.parts]
    col_name = parts[-1]
    if not col_name:
        return None
    qualifier = [part.lower() for part in parts[:-1]]
    if not qualifier:
        return col_name
    source_lower = tuple(part.lower() for part in source_parts)
    if len(qualifier) == 1:
        return (
            col_name
            if qualifier[0] in {dataset_name.lower(), source_lower[-1]}
            else None
        )
    if len(qualifier) == 2 and len(source_lower) >= 2:
        return col_name if tuple(qualifier) == source_lower[-2:] else None
    if len(qualifier) == 3 and len(source_lower) == 3:
        return col_name if tuple(qualifier) == source_lower else None
    return None


def _merge_text_parts(additions: list[str]) -> str:
    seen: list[str] = []
    for chunk in additions:
        for piece in chunk.split("；"):
            cleaned = piece.strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
    return "；".join(seen)


def _ai_context_parts(ai_context: Any) -> tuple[str, list[str], list[str]]:
    if isinstance(ai_context, str):
        return ai_context.strip(), [], []
    if isinstance(ai_context, dict):
        instructions = str(ai_context.get("instructions") or "").strip()
        synonyms = [
            str(s).strip() for s in (ai_context.get("synonyms") or []) if str(s).strip()
        ]
        examples = [
            str(e).strip() for e in (ai_context.get("examples") or []) if str(e).strip()
        ]
        return instructions, synonyms, examples
    return "", [], []


def _extract_semantica_extension(
    field_obj: dict[str, Any],
) -> tuple[bool, str | None]:
    partition = False
    raw_type: str | None = None
    for ext in field_obj.get("nonportable_extensions") or []:
        if ext.get("vendor_name") != SEMANTICA_VENDOR_NAME:
            continue
        data = ext.get("data") or {}
        if "partition" in data:
            if not isinstance(data["partition"], bool):
                raise ValueError(
                    f"Field {field_obj['name']!r} SEMANTICA_DATAAGENT.partition must be a boolean"
                )
            partition = data["partition"]
        if "raw_type" in data and data["raw_type"] is not None:
            if not isinstance(data["raw_type"], str) or not data["raw_type"].strip():
                raise ValueError(
                    f"Field {field_obj['name']!r} SEMANTICA_DATAAGENT.raw_type must be a non-empty string"
                )
            raw_type = data["raw_type"].strip()
    return partition, raw_type


def _full_table_name(table_key: tuple[str, str, str]) -> str:
    return ".".join(part for part in table_key if part)


def _safe_doc_stem(model_name: str, used_stems: set[str]) -> str:
    cleaned = re.sub(r"[\\/\x00-\x1f\x7f]+", "_", model_name).strip().lstrip(".")
    cleaned = re.sub(r"\.\.+", "_", cleaned)
    if not cleaned:
        cleaned = "model"
    if cleaned.lower() == "index":
        cleaned = "index_model"
    stem = cleaned
    if stem in used_stems:
        digest = hashlib.sha256(model_name.encode("utf-8")).hexdigest()[:8]
        stem = f"{cleaned}_{digest}"
    used_stems.add(stem)
    return stem


def _render_ai_context_lines(lines: list[str], ai_context: Any) -> None:
    instructions, synonyms, examples = _ai_context_parts(ai_context)
    if instructions:
        lines.append(f"- AI 说明：{instructions}")
    if synonyms:
        lines.append(f"- 同义词：{', '.join(synonyms)}")
    if examples:
        lines.append(f"- 示例：{'; '.join(examples)}")


def _render_metric_lines(lines: list[str], metric: dict[str, Any]) -> None:
    dtype = metric.get("datatype")
    header = f"#### {metric['name']}" + (f"（`{dtype}`）" if dtype else "")
    lines.append(header)
    if metric.get("description"):
        lines.append(metric["description"].strip())
    for dialect, expr in _all_expressions(metric["expression"]):
        lines.append(f"- 表达式（{dialect}）：`{expr}`")
    _render_ai_context_lines(lines, metric.get("ai_context"))
    lines.append("")


def _render_model_document(
    model: dict[str, Any],
    source_file: str,
    dataset_tables: dict[str, tuple[str, str, str]],
    computed_fields: dict[str, list[dict[str, Any]]],
    physical_synonyms: dict[str, list[str]],
    relationship_notes: list[dict[str, Any]],
) -> str:
    lines: list[str] = [f"# {model['name']}", "", f"- 来源模型文件：`{source_file}`"]
    if model.get("description"):
        lines.append(f"- 模型说明：{model['description'].strip()}")
    _render_ai_context_lines(lines, model.get("ai_context"))
    lines.extend(["", "## 数据集（Datasets）", ""])
    for ds in model["datasets"]:
        ds_name = ds["name"]
        full_name = _full_table_name(dataset_tables[ds_name])
        lines.extend([f"### {ds_name}（`{full_name}`）", f"- 物理表：`{full_name}`"])
        if ds.get("description"):
            lines.append(f"- 数据集说明：{ds['description'].strip()}")
        _render_ai_context_lines(lines, ds.get("ai_context"))
        if physical_synonyms.get(ds_name):
            lines.append(
                "- 物理字段同义词与逻辑别名：" + "; ".join(physical_synonyms[ds_name])
            )
        lines.append("")
        _render_computed_fields(lines, computed_fields.get(ds_name) or [])
        if ds.get("metrics"):
            lines.extend(["#### 数据集指标（Dataset Metrics）", ""])
            for metric in ds["metrics"]:
                _render_metric_lines(lines, metric)

    if relationship_notes:
        _render_relationships_section(lines, relationship_notes)
    if model.get("metrics"):
        lines.extend(["## 模型指标（Model Metrics）", ""])
        for metric in model["metrics"]:
            _render_metric_lines(lines, metric)

    return "\n".join(lines).rstrip() + "\n"


def _render_computed_fields(
    lines: list[str], ds_computed: list[dict[str, Any]]
) -> None:
    if not ds_computed:
        return
    lines.extend(["#### 计算字段（Calculated Fields）", ""])
    for f in ds_computed:
        dtype = f.get("datatype")
        title = f"- **{f['name']}**"
        if f.get("label"):
            title += f"（{f['label'].strip()}）"
        if dtype:
            title += f" [`{dtype}`]"
        lines.append(title)
        if f.get("description"):
            lines.append(f"  - 说明：{f['description'].strip()}")
        for dialect, expr in _all_expressions(f["expression"]):
            lines.append(f"  - 表达式（{dialect}）：`{expr}`")
        _, f_syns, _ = _ai_context_parts(f.get("ai_context"))
        if f_syns:
            lines.append(f"  - 同义词：{', '.join(f_syns)}")
    lines.append("")


def _render_relationships_section(
    lines: list[str], relationship_notes: list[dict[str, Any]]
) -> None:
    lines.extend(["## 表间关系（Relationships）", ""])
    for note in relationship_notes:
        status = " [计算字段关系：未生成物理外键]" if note["calculated"] else ""
        lines.append(
            f"- **{note['name']}**{status}：`{note['from_ds']}`（`{note['from_table']}`）"
            f" -> `{note['to_ds']}`（`{note['to_table']}`）"
        )
        lines.append(
            f"  - 逻辑列：`{', '.join(note['from_columns'])}` -> `{', '.join(note['to_columns'])}`"
        )
        if not note["calculated"]:
            lines.append(
                f"  - 物理列：`{', '.join(note['physical_from'])}` -> `{', '.join(note['physical_to'])}`"
            )
        _render_ai_context_lines(lines, note.get("ai_context"))
    lines.append("")


def _render_index_document(entries: list[dict[str, str]]) -> str:
    lines = [
        "# Apache Ossie 语义模型索引地图",
        "",
        "由 Apache Ossie 源适配器自动生成，汇总当前知识类下的语义模型文档、计算字段、指标口径与关系说明。",
        "",
    ]
    for item in entries:
        summary = f" — {item['description']}" if item["description"] else ""
        lines.append(f"- {item['path']}：{item['name']}{summary}")
    return "\n".join(lines) + "\n"


@dataclass
class _MergedTable:
    catalog: str | None
    database: str
    name: str
    comment_parts: list[str] = field(default_factory=list)
    source_files: set[str] = field(default_factory=set)


@dataclass
class _MergedField:
    catalog: str | None
    database: str
    table: str
    name: str
    field_type: str
    partition: bool
    comment_parts: list[str] = field(default_factory=list)
    source_files: set[str] = field(default_factory=set)


@dataclass
class _DatasetMeta:
    table_key: tuple[str, str, str]
    catalog: str | None
    database: str
    table_name: str
    logical_to_physical: dict[str, str | None]


def _collect_model_files(base: Path, rel_root: Path) -> list[Path]:
    root = safe(base, rel_root)
    if not root.exists():
        return []
    base_resolved = base.resolve()
    if not root.is_dir() or not root.resolve().is_relative_to(base_resolved):
        raise ValueError("Ossie root is not a directory within source directory")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(base).parts
        if (
            ".git" in rel_parts
            or (rel_parts and rel_parts[0] == "overrides")
            or any(part.startswith(".") for part in path.relative_to(root).parts)
        ):
            continue
        if path.is_dir():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(base_resolved):
            raise ValueError(f"Ossie file escapes source directory: {path}")
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS:
            files.append(path)
    return files


def _merge_dataset(
    ds: dict[str, Any],
    rel_file: str,
    strip_catalog: bool,
    merged_tables: dict[tuple[str, str, str], _MergedTable],
    merged_fields: dict[tuple[tuple[str, str, str], str], _MergedField],
    stats: Counter[str],
) -> tuple[_DatasetMeta, list[dict[str, Any]], list[str]]:
    ds_name = ds["name"]
    catalog, database, table_name, raw_source_parts = _parse_dataset_source(
        ds["source"], strip_catalog=strip_catalog
    )
    table_key = (catalog or "", database, table_name)
    table_entry = merged_tables.setdefault(
        table_key, _MergedTable(catalog=catalog, database=database, name=table_name)
    )
    table_entry.source_files.add(rel_file)

    ds_instructions, ds_synonyms, _ = _ai_context_parts(ds.get("ai_context"))
    ds_comment_pieces = [
        str(ds.get("description") or "").strip(),
        ds_instructions,
        f"同义词: {', '.join(ds_synonyms)}" if ds_synonyms else "",
    ]
    table_entry.comment_parts.extend(p for p in ds_comment_pieces if p)

    ds_field_names: set[str] = set()
    ds_metric_names: set[str] = set()
    logical_to_physical: dict[str, str | None] = {}
    computed: list[dict[str, Any]] = []
    synonyms_summary: list[str] = []

    for field_obj in ds.get("fields") or []:
        fname = field_obj["name"]
        if fname in ds_field_names:
            raise ValueError(
                f"{rel_file}: Duplicate field name {fname!r} in dataset {ds_name!r}"
            )
        ds_field_names.add(fname)

        expr_text, expr_dialect = _select_expression(field_obj["expression"])
        physical_col = extract_single_column(
            expr_text, expr_dialect, ds_name, raw_source_parts
        )
        logical_to_physical[fname] = physical_col
        if physical_col is None:
            stats["calculated_fields"] += 1
            computed.append(field_obj)
            continue

        partition, raw_type = _extract_semantica_extension(field_obj)
        field_type = raw_type or DATATYPE_MAP.get(field_obj.get("datatype"), "string")
        f_instructions, f_synonyms, _ = _ai_context_parts(field_obj.get("ai_context"))
        desc = str(field_obj.get("description") or "").strip()
        label = str(field_obj.get("label") or "").strip()
        comment_pieces = [
            desc,
            f"标签: {label}" if label and label != desc else "",
            f"逻辑名: {fname}" if fname != physical_col else "",
            f"同义词: {', '.join(f_synonyms)}" if f_synonyms else "",
            f_instructions,
        ]
        if f_synonyms or fname != physical_col or label:
            hints = [
                item
                for item in (
                    f"逻辑名 {fname}" if fname != physical_col else "",
                    f"标签 {label}" if label else "",
                    f"同义词 {', '.join(f_synonyms)}" if f_synonyms else "",
                )
                if item
            ]
            synonyms_summary.append(f"`{physical_col}`（{'；'.join(hints)}）")

        field_key = (table_key, physical_col)
        existing_field = merged_fields.get(field_key)
        if existing_field is None:
            merged_fields[field_key] = _MergedField(
                catalog=catalog,
                database=database,
                table=table_name,
                name=physical_col,
                field_type=field_type,
                partition=partition,
                comment_parts=[p for p in comment_pieces if p],
                source_files={rel_file},
            )
        else:
            table_full = _full_table_name(table_key)
            if existing_field.field_type != field_type:
                raise ValueError(
                    f"Conflicting field type for {table_full}.{physical_col}: "
                    f"{existing_field.field_type!r} vs {field_type!r}"
                )
            if existing_field.partition != partition:
                raise ValueError(
                    f"Conflicting partition flag for {table_full}.{physical_col}: "
                    f"{existing_field.partition!r} vs {partition!r}"
                )
            existing_field.comment_parts.extend(p for p in comment_pieces if p)
            existing_field.source_files.add(rel_file)

    for metric_obj in ds.get("metrics") or []:
        mname = metric_obj["name"]
        if mname in ds_field_names or mname in ds_metric_names:
            raise ValueError(
                f"{rel_file}: Duplicate field/metric name {mname!r} in dataset {ds_name!r}"
            )
        ds_metric_names.add(mname)
        stats["metrics"] += 1

    meta = _DatasetMeta(
        table_key=table_key,
        catalog=catalog,
        database=database,
        table_name=table_name,
        logical_to_physical=logical_to_physical,
    )
    return meta, computed, synonyms_summary


def _resolve_relationships(
    raw_relationships: list[dict[str, Any]],
    rel_file: str,
    dataset_meta: dict[str, _DatasetMeta],
    stats: Counter[str],
) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    seen_names: set[str] = set()
    resolved: list[tuple[dict[str, Any], str]] = []
    notes: list[dict[str, Any]] = []
    for rel_obj in raw_relationships:
        rname = rel_obj["name"]
        if rname in seen_names:
            raise ValueError(f"{rel_file}: Duplicate relationship name {rname!r}")
        seen_names.add(rname)

        from_ds, to_ds = rel_obj["from"], rel_obj["to"]
        if from_ds not in dataset_meta:
            raise ValueError(
                f"{rel_file}: Relationship {rname!r} refers to unknown dataset {from_ds!r}"
            )
        if to_ds not in dataset_meta:
            raise ValueError(
                f"{rel_file}: Relationship {rname!r} refers to unknown dataset {to_ds!r}"
            )
        from_cols, to_cols = rel_obj["from_columns"], rel_obj["to_columns"]
        if len(from_cols) != len(to_cols):
            raise ValueError(
                f"{rel_file}: Relationship {rname!r} has mismatched column counts"
            )
        from_meta, to_meta = dataset_meta[from_ds], dataset_meta[to_ds]
        for col in from_cols:
            if col not in from_meta.logical_to_physical:
                raise ValueError(
                    f"{rel_file}: Relationship {rname!r} refers to unknown field {col!r} on dataset {from_ds!r}"
                )
        for col in to_cols:
            if col not in to_meta.logical_to_physical:
                raise ValueError(
                    f"{rel_file}: Relationship {rname!r} refers to unknown field {col!r} on dataset {to_ds!r}"
                )

        mapped_from = [from_meta.logical_to_physical[c] for c in from_cols]
        mapped_to = [to_meta.logical_to_physical[c] for c in to_cols]
        is_calculated = any(col is None for col in mapped_from + mapped_to)

        notes.append(
            {
                "name": rname,
                "from_ds": from_ds,
                "to_ds": to_ds,
                "from_table": _full_table_name(from_meta.table_key),
                "to_table": _full_table_name(to_meta.table_key),
                "from_columns": from_cols,
                "to_columns": to_cols,
                "physical_from": [c for c in mapped_from if c is not None],
                "physical_to": [c for c in mapped_to if c is not None],
                "calculated": is_calculated,
                "ai_context": rel_obj.get("ai_context"),
            }
        )
        if is_calculated:
            stats["skipped_calculated_relationships"] += 1
            continue

        rel_data: dict[str, Any] = {
            "name": rname,
            "from_database": from_meta.database,
            "from_table": from_meta.table_name,
            "from_columns": [c for c in mapped_from if c is not None],
            "to_database": to_meta.database,
            "to_table": to_meta.table_name,
            "to_columns": [c for c in mapped_to if c is not None],
        }
        if from_meta.catalog is not None:
            rel_data["from_catalog"] = from_meta.catalog
        if to_meta.catalog is not None:
            rel_data["to_catalog"] = to_meta.catalog
        resolved.append((rel_data, rel_file))
        stats["relationships"] += 1
    return resolved, notes


def _build_canonical_records(
    merged_tables: dict[tuple[str, str, str], _MergedTable],
    merged_fields: dict[tuple[tuple[str, str, str], str], _MergedField],
    relationships: list[tuple[dict[str, Any], str]],
    stats: Counter[str],
) -> list[Record]:
    records: list[Record] = []
    for table_key in sorted(merged_tables):
        entry = merged_tables[table_key]
        files = tuple(sorted(entry.source_files))
        data: dict[str, Any] = {
            "database": entry.database,
            "name": entry.name,
            "comment": _merge_text_parts(entry.comment_parts),
        }
        if entry.catalog is not None:
            data["catalog"] = entry.catalog
        records.append(Record("table", data, files[0], None, files))
        stats["tables"] += 1

    for field_key in sorted(merged_fields):
        f_entry = merged_fields[field_key]
        files = tuple(sorted(f_entry.source_files))
        data = {
            "database": f_entry.database,
            "table": f_entry.table,
            "name": f_entry.name,
            "type": f_entry.field_type,
            "comment": _merge_text_parts(f_entry.comment_parts),
            "partition": f_entry.partition,
        }
        if f_entry.catalog is not None:
            data["catalog"] = f_entry.catalog
        records.append(Record("field", data, files[0], None, files))
        stats["fields"] += 1

    relationships.sort(
        key=lambda item: (
            item[0].get("from_catalog") or "",
            item[0]["from_database"],
            item[0]["from_table"],
            item[0].get("to_catalog") or "",
            item[0]["to_database"],
            item[0]["to_table"],
            item[0]["name"],
        )
    )
    for rel_data, rel_file in relationships:
        records.append(Record("relationship", rel_data, rel_file, None, (rel_file,)))
    return records


class OssieAdapter:
    """Read Apache Ossie YAML/JSON semantic models into canonical records."""

    id = "ossie"
    version = "1.0.0"
    display_name = "Apache Ossie"
    project_types = ("warehouse",)
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "default": "ossie",
                "description": "Directory holding Apache Ossie model files, relative to the source directory.",
            },
            "category": {
                "type": "string",
                "minLength": 1,
                "default": "ossie",
                "description": "Knowledge category name for derived Ossie documentation.",
            },
            "strip_catalog": {
                "type": "boolean",
                "default": False,
                "description": "Strip the leading catalog segment from three-part dataset sources.",
            },
        },
        "additionalProperties": False,
    }

    def owned_paths(self, parameters: dict) -> tuple[str, ...]:
        """Return relative subdirectories owned by this adapter."""
        raw = parameters.get("path", "ossie")
        return (raw,) if isinstance(raw, str) and raw else ()

    def read(self, directory: Path, parameters: dict) -> ReadResult:
        """Parse, validate and split Ossie models into canonical records."""
        Draft202012Validator(self.parameters_schema).validate(parameters)
        base = Path(directory)
        if not base.is_dir():
            raise ValueError(f"Source directory does not exist: {base}")

        rel_root = safe_name(parameters.get("path", "ossie"))
        category = category_path(parameters.get("category", "ossie")).as_posix()
        strip_catalog = bool(parameters.get("strip_catalog", False))

        model_files = _collect_model_files(base, rel_root)
        if not model_files:
            return ReadResult([], {})

        hashes: dict[str, str] = {}
        stats: Counter[str] = Counter()
        merged_tables: dict[tuple[str, str, str], _MergedTable] = {}
        merged_fields: dict[tuple[tuple[str, str, str], str], _MergedField] = {}
        relationships: list[tuple[dict[str, Any], str]] = []
        doc_records: list[Record] = []
        index_entries: list[dict[str, str]] = []
        seen_models: set[str] = set()
        used_doc_stems: set[str] = set()
        model_source_files: list[str] = []

        for file_path in model_files:
            rel_file = file_path.relative_to(base).as_posix()
            raw = file_path.read_bytes()
            hashes[rel_file] = hashlib.sha256(raw).hexdigest()
            try:
                model = _normalize_and_validate_model(
                    _load_raw_document(file_path, raw), rel_file
                )
            except ValueError as exc:
                if str(exc).startswith(f"{rel_file}:"):
                    raise
                raise ValueError(f"{rel_file}: {exc}") from exc

            model_name = model["name"]
            if model_name in seen_models:
                raise ValueError(f"Duplicate Ossie model name: {model_name!r}")
            seen_models.add(model_name)
            model_source_files.append(rel_file)
            stats["models"] += 1

            dataset_meta: dict[str, _DatasetMeta] = {}
            computed_by_ds: dict[str, list[dict[str, Any]]] = {}
            synonyms_by_ds: dict[str, list[str]] = {}
            for ds in model["datasets"]:
                ds_name = ds["name"]
                if ds_name in dataset_meta:
                    raise ValueError(
                        f"{rel_file}: Duplicate dataset name {ds_name!r} in model {model_name!r}"
                    )
                meta, computed, syns = _merge_dataset(
                    ds, rel_file, strip_catalog, merged_tables, merged_fields, stats
                )
                dataset_meta[ds_name] = meta
                if computed:
                    computed_by_ds[ds_name] = computed
                if syns:
                    synonyms_by_ds[ds_name] = syns

            seen_model_metrics: set[str] = set()
            for metric_obj in model.get("metrics") or []:
                mname = metric_obj["name"]
                if mname in seen_model_metrics:
                    raise ValueError(
                        f"{rel_file}: Duplicate model metric name {mname!r}"
                    )
                seen_model_metrics.add(mname)
                stats["metrics"] += 1

            rel_items, rel_notes = _resolve_relationships(
                model.get("relationships") or [], rel_file, dataset_meta, stats
            )
            relationships.extend(rel_items)

            doc_path = f"{_safe_doc_stem(model_name, used_doc_stems)}.md"
            doc_content = _render_model_document(
                model=model,
                source_file=rel_file,
                dataset_tables={k: v.table_key for k, v in dataset_meta.items()},
                computed_fields=computed_by_ds,
                physical_synonyms=synonyms_by_ds,
                relationship_notes=rel_notes,
            )
            doc_source_key = f"{rel_root.as_posix()}/__derived__/{category}/{doc_path}"
            hashes[doc_source_key] = hashlib.sha256(
                doc_content.encode("utf-8")
            ).hexdigest()
            doc_records.append(
                Record(
                    "document",
                    {
                        "category": category,
                        "path": doc_path,
                        "content": doc_content,
                        "generated": True,
                    },
                    rel_file,
                    doc_source_key,
                    (rel_file,),
                )
            )
            index_entries.append(
                {
                    "path": doc_path,
                    "name": model_name,
                    "description": str(model.get("description") or "").strip(),
                }
            )
            stats["documents"] += 1

        records = _build_canonical_records(
            merged_tables, merged_fields, relationships, stats
        )
        index_content = _render_index_document(index_entries)
        index_source_key = f"{rel_root.as_posix()}/__derived__/{category}/index.md"
        hashes[index_source_key] = hashlib.sha256(
            index_content.encode("utf-8")
        ).hexdigest()
        sorted_sources = tuple(sorted(model_source_files))
        doc_records.append(
            Record(
                "document",
                {
                    "category": category,
                    "path": "index.md",
                    "content": index_content,
                    "generated": True,
                },
                sorted_sources[0],
                index_source_key,
                sorted_sources,
            )
        )
        stats["documents"] += 1

        records.extend(doc_records)
        stats["files_hashed"] = len(hashes)
        return ReadResult(records, hashes, dict(stats))
