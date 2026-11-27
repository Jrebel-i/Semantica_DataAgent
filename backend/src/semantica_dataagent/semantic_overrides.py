"""Git-backed structured semantic overrides, L1 static validation, and SQLite migration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

import sqlglot
import yaml
from jsonschema import Draft202012Validator
from sqlglot import exp

from .canonical import Record
from .db import MetadataOverride, sessions

logger = logging.getLogger(__name__)

OVERRIDE_RELATIVE_PATH = "overrides/semantic_overrides.yaml"
OVERRIDE_METRICS_DOC_PATH = "overrides/semantic_overrides/metrics.md"
OVERRIDE_METRICS_CATEGORY = "语义覆盖指标"
LEGACY_MIGRATION_AUTHOR = "系统迁移"

_SECTION_KEYS = ("tables", "fields", "relationships", "metrics")

_DIALECT_MAP: dict[str | None, str | None] = {
    None: None,
    "": None,
    "hive": "hive",
    "spark": "spark",
    "doris": "doris",
    "OSSIE_SQL_2026": "spark",
    "ANSI_SQL": None,
}

_FORBIDDEN_SQL_NODES = tuple(
    node
    for node in (
        getattr(exp, "Drop", None),
        getattr(exp, "Delete", None),
        getattr(exp, "Insert", None),
        getattr(exp, "Update", None),
        getattr(exp, "Create", None),
        getattr(exp, "Alter", None),
        getattr(exp, "Merge", None),
        getattr(exp, "Command", None),
        getattr(exp, "TruncateTable", None),
        getattr(exp, "Pragma", None),
    )
    if node is not None
)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "api_key",
        re.compile(
            r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})"
        ),
    ),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{12,}")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api_key|apikey|access_key|secret_key|auth_token|private_key)\b\s*[:=]\s*['\"]?[^\s'\"]{4,}"
        ),
    ),
    ("connection_string", re.compile(r"[a-z][a-z0-9+.-]*://[^:\s/@]+:[^@\s/@]+@")),
)

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+(?:instructions|prompts?|rules?)\b"
    ),
    re.compile(
        r"(?i)\b(?:override|bypass)\s+(?:the\s+)?system\s+(?:prompt|instructions|rules?)\b"
    ),
    re.compile(r"(?i)(?:<\|im_start\|>|<\|system\|>|\[INST\]|<<SYS>>)"),
    re.compile(
        r"(?:忽略|无视|跳过|覆盖)(?:之前|以前|上述|以上|全部|所有)+(?:的)?(?:指令|提示词?|规则|约束|系统设定)"
    ),
    re.compile(r"(?:泄露|输出|打印|返回)(?:系统提示词?|全部密钥|密码|环境变量)"),
)


def scrub_untrusted_text(value: str) -> str:
    """Redact credentials/secrets and neutralize prompt-injection directives in text."""
    cleaned = str(value or "")
    for _, pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    for pattern in _INJECTION_PATTERNS:
        cleaned = pattern.sub("[FILTERED]", cleaned)
    return cleaned


@dataclass(frozen=True)
class OverrideApplyResult:
    """Result of overlaying structured semantic overrides onto build records."""

    records: list[Record]
    stale_overrides: list[dict[str, str]] = field(default_factory=list)
    applied_count: int = 0


@dataclass(frozen=True)
class GateReport:
    """Result of running L1 deterministic validation checks on a correction."""

    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize gate results for database persistence and API responses."""
        return {
            "passed": self.passed,
            "checks": list(self.checks),
            "errors": list(self.errors),
        }


class GateValidationError(ValueError):
    """Raised when a proposal fails L1 deterministic static validation."""

    def __init__(self, report: GateReport):
        self.report = report
        super().__init__("; ".join(report.errors) or "L1 static validation failed")


@dataclass
class _SchemaIndex:
    """In-memory index of physical tables and their column names in a generation."""

    tables: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    by_db_table: dict[tuple[str, str], list[tuple[str, str, str]]] = field(
        default_factory=dict
    )
    by_name: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: list[Record]) -> _SchemaIndex:
        index = cls()
        for record in records:
            if record.kind == "table":
                key = (
                    _norm_cat(record.data.get("catalog")),
                    record.data["database"],
                    record.data["name"],
                )
                index._register_table(key)
        for record in records:
            if record.kind == "field":
                key = (
                    _norm_cat(record.data.get("catalog")),
                    record.data["database"],
                    record.data["table"],
                )
                index._register_table(key)
                index.tables[key].add(record.data["name"])
        return index

    @classmethod
    def from_described(cls, tables: list[dict[str, Any]]) -> _SchemaIndex:
        index = cls()
        for table in tables:
            key = (
                _norm_cat(table.get("catalog")),
                str(table.get("database") or ""),
                str(table.get("name") or ""),
            )
            index._register_table(key)
            for col in table.get("fields") or ():
                cname = str(col.get("name") or "")
                if cname:
                    index.tables[key].add(cname)
        return index

    def _register_table(self, key: tuple[str, str, str]) -> None:
        if key not in self.tables:
            self.tables[key] = set()
            self.by_db_table.setdefault((key[1], key[2]), []).append(key)
            self.by_name.setdefault(key[2], []).append(key)

    def match_table(
        self, catalog: str | None, database: str, name: str
    ) -> list[tuple[str, str, str]]:
        cat = _norm_cat(catalog)
        if cat:
            key = (cat, database, name)
            return [key] if key in self.tables else []
        candidates = self.by_db_table.get((database, name), [])
        empty = [c for c in candidates if c[0] == ""]
        return empty or list(candidates)

    def candidates(
        self, catalog: str | None, database: str | None, name: str
    ) -> list[tuple[str, str, str]]:
        if _norm_cat(catalog):
            return self.match_table(catalog, database or "", name)
        if database:
            return list(self.by_db_table.get((database, name), []))
        return list(self.by_name.get(name, []))

    def resolve_table_ref(self, ref: str) -> list[tuple[str, str, str]]:
        parts = [p.strip() for p in ref.split(".") if p.strip()]
        if len(parts) == 3:
            return self.match_table(parts[0], parts[1], parts[2])
        if len(parts) == 2:
            return self.match_table(None, parts[0], parts[1])
        if len(parts) == 1:
            return list(self.by_name.get(parts[0], []))
        return []


def _write_ambiguities(patch: dict[str, Any], index: _SchemaIndex) -> list[str]:
    errors: list[str] = []

    def check(label: str, catalog: str | None, database: str | None, name: str) -> None:
        candidates = index.candidates(catalog, database, name)
        if not _norm_cat(catalog) and len({key[0] for key in candidates}) > 1:
            names = [
                ".".join(part for part in key if part) for key in sorted(candidates)
            ]
            errors.append(
                f"{label}: ambiguous table; specify catalog: {', '.join(names)}"
            )

    for key, entry in patch["tables"].items():
        check(f"table '{key}'", entry.get("catalog"), entry["database"], entry["name"])
    for key, entry in patch["fields"].items():
        check(f"field '{key}'", entry.get("catalog"), entry["database"], entry["table"])
    for key, entry in patch["relationships"].items():
        for side in ("from", "to"):
            check(
                f"relationship '{key}' {side}",
                entry.get(f"{side}_catalog"),
                entry[f"{side}_database"],
                entry[f"{side}_table"],
            )
    for key, entry in patch["metrics"].items():
        refs = ([entry["table"]] if entry.get("table") else []) + list(
            entry.get("tables") or []
        )
        for ref in refs:
            parts = str(ref).split(".")
            if len(parts) == 1:
                check(f"metric '{key}'", None, None, parts[0])
            elif len(parts) == 2:
                check(f"metric '{key}'", None, parts[0], parts[1])
        for sql in [entry["expression"], *(entry.get("filters") or [])]:
            try:
                parsed = sqlglot.parse_one(
                    str(sql), read=_DIALECT_MAP.get(entry.get("dialect"))
                )
            except sqlglot.errors.SqlglotError:
                continue
            if parsed is not None:
                for col in parsed.find_all(exp.Column):
                    parts = [p.name for p in col.parts if getattr(p, "name", None)]
                    if len(parts) == 3:
                        scoped = [
                            candidate
                            for ref in refs
                            for candidate in index.resolve_table_ref(str(ref))
                            if candidate[1:] == (parts[0], parts[1])
                        ]
                        if not scoped:
                            check(f"metric '{key}'", None, parts[0], parts[1])
                    elif len(parts) == 2:
                        scoped = [
                            candidate
                            for ref in refs
                            for candidate in index.resolve_table_ref(str(ref))
                            if candidate[2] == parts[0]
                        ]
                        if not scoped:
                            check(f"metric '{key}'", None, None, parts[0])
    return errors


def _norm_cat(value: str | None) -> str:
    return str(value or "").strip()


def _schema_path() -> Path:
    packaged = (
        Path(__file__).resolve().parent
        / "schemas"
        / "v2"
        / "semantic_override.schema.json"
    )
    if packaged.is_file():
        return packaged
    return (
        Path(__file__).resolve().parents[3]
        / "schemas"
        / "v2"
        / "semantic_override.schema.json"
    )


@lru_cache(maxsize=1)
def load_override_schema() -> dict[str, Any]:
    """Load the JSON Schema for ``overrides/semantic_overrides.yaml``."""
    return json.loads(_schema_path().read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _override_validator() -> Draft202012Validator:
    return Draft202012Validator(load_override_schema())


def empty_overrides_doc() -> dict[str, Any]:
    """Return an empty version-1 structured semantic overrides document."""
    return {
        "version": 1,
        "tables": {},
        "fields": {},
        "relationships": {},
        "metrics": {},
    }


def validate_override_document(doc: Any) -> list[str]:
    """Return a list of schema validation errors for a semantic override document."""
    if not isinstance(doc, dict):
        return ["Semantic overrides root must be a mapping object"]
    errors: list[str] = []
    for err in sorted(
        _override_validator().iter_errors(doc), key=lambda e: list(e.path)
    ):
        loc = ".".join(str(p) for p in err.path) or "<root>"
        errors.append(f"{loc}: {err.message}")
    return errors


def normalize_override_document(raw: Any) -> dict[str, Any]:
    """Normalize a parsed YAML/JSON dict into canonical version-1 override sections."""
    if raw is None:
        return empty_overrides_doc()
    candidate = dict(raw) if isinstance(raw, dict) else None
    if candidate is None:
        raise ValueError("Semantic overrides document must be a YAML mapping")
    if "version" not in candidate:
        candidate["version"] = 1
    errors = validate_override_document(candidate)
    if errors:
        raise ValueError(f"Invalid semantic overrides document: {errors[0]}")
    out = empty_overrides_doc()
    for section in _SECTION_KEYS:
        section_val = candidate.get(section)
        if isinstance(section_val, dict):
            out[section] = {str(k): dict(v) for k, v in section_val.items()}
    return out


def normalize_structured_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a structured patch payload against the override schema."""
    normalized = normalize_override_document(patch)
    if not any(normalized[s] for s in _SECTION_KEYS):
        raise ValueError(
            "structured_patch must declare at least one entry in "
            "tables, fields, relationships, or metrics"
        )
    return normalized


def override_file_path(root: Path) -> Path:
    """Return the path to ``overrides/semantic_overrides.yaml`` in a source directory."""
    return root / OVERRIDE_RELATIVE_PATH


def load_overrides_file(root: Path) -> tuple[dict[str, Any], str | None]:
    """Load and validate ``overrides/semantic_overrides.yaml`` if present."""
    path = override_file_path(root)
    if not path.is_file():
        return empty_overrides_doc(), None
    raw_bytes = path.read_bytes()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    parsed = yaml.safe_load(raw_bytes.decode("utf-8"))
    return normalize_override_document(parsed), sha


def dump_overrides_yaml(doc: dict[str, Any]) -> str:
    """Serialize a normalized semantic override document deterministically to YAML."""
    normalized = normalize_override_document(doc)
    payload: dict[str, Any] = {"version": 1}
    for section in _SECTION_KEYS:
        if normalized[section]:
            payload[section] = normalized[section]
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def merge_overrides_doc(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a validated structured patch on top of an existing override document."""
    base_norm = normalize_override_document(base)
    patch_norm = normalize_structured_patch(patch)
    merged = {
        "version": 1,
        "tables": {k: dict(v) for k, v in base_norm["tables"].items()},
        "fields": {k: dict(v) for k, v in base_norm["fields"].items()},
        "relationships": {k: dict(v) for k, v in base_norm["relationships"].items()},
        "metrics": {k: dict(v) for k, v in base_norm["metrics"].items()},
    }
    for section in _SECTION_KEYS:
        for key, entry in patch_norm[section].items():
            if section in {"tables", "fields"} and key in merged[section]:
                merged[section][key].update(entry)
            else:
                merged[section][key] = dict(entry)
    return normalize_override_document(merged)


def legacy_target_to_patch(target: str, patch_text: str) -> dict[str, Any] | None:
    """Convert a legacy ``table:`` or ``field:`` target correction into a structured patch."""
    comment = patch_text.strip()
    if target.startswith("table:"):
        ident = target[len("table:") :].strip()
        parts = [p for p in ident.split(".") if p]
        if len(parts) == 2:
            db_name, tbl_name = parts
            entry: dict[str, Any] = {
                "database": db_name,
                "name": tbl_name,
                "comment": comment,
            }
        elif len(parts) == 3:
            cat_name, db_name, tbl_name = parts
            entry = {
                "catalog": cat_name,
                "database": db_name,
                "name": tbl_name,
                "comment": comment,
            }
        else:
            return None
        return {"version": 1, "tables": {ident: entry}}
    if target.startswith("field:"):
        ident = target[len("field:") :].strip()
        parts = [p for p in ident.split(".") if p]
        if len(parts) == 3:
            db_name, tbl_name, fld_name = parts
            entry = {
                "database": db_name,
                "table": tbl_name,
                "name": fld_name,
                "comment": comment,
            }
        elif len(parts) == 4:
            cat_name, db_name, tbl_name, fld_name = parts
            entry = {
                "catalog": cat_name,
                "database": db_name,
                "table": tbl_name,
                "name": fld_name,
                "comment": comment,
            }
        else:
            return None
        return {"version": 1, "fields": {ident: entry}}
    return None


def _table_entry_keys(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _norm_cat(entry.get("catalog")),
        str(entry["database"]),
        str(entry["name"]),
    )


def _field_entry_keys(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _norm_cat(entry.get("catalog")),
        str(entry["database"]),
        str(entry["table"]),
        str(entry["name"]),
    )


def _iter_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path or "<text>", value))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(_iter_strings(item, f"{path}[{idx}]"))
    elif isinstance(value, dict):
        for k, v in value.items():
            sub = f"{path}.{k}" if path else str(k)
            found.extend(_iter_strings(v, sub))
    return found


def scan_secrets_and_injections(payload: Any) -> list[str]:
    """Detect credential/secret leaks and prompt-injection patterns in text fields."""
    errors: list[str] = []
    for path, text in _iter_strings(payload):
        if not text:
            continue
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(
                    f"Detected potential secret/credential ({kind}) in {path}"
                )
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                errors.append(f"Detected prompt-injection pattern in {path}")
    return errors


def _validate_sql_fragment(
    sql: str,
    *,
    dialect: str | None,
    wrap_prefix: str,
    schema_index: _SchemaIndex,
    scope_tables: list[tuple[str, str, str]],
    check_columns: bool,
) -> list[str]:
    errors: list[str] = []
    mapped_dialect = _DIALECT_MAP.get(dialect)
    try:
        statements = sqlglot.parse(sql, read=mapped_dialect)
    except sqlglot.errors.SqlglotError as exc:
        return [f"SQL syntax error: {exc}"]
    if len(statements) != 1 or statements[0] is None:
        return ["SQL expression must contain exactly one statement"]
    for node in statements[0].walk():
        if isinstance(node, _FORBIDDEN_SQL_NODES):
            return [f"Disallowed SQL statement/command node: {type(node).__name__}"]

    try:
        wrapped = sqlglot.parse_one(f"{wrap_prefix} {sql}", read=mapped_dialect)
    except sqlglot.errors.SqlglotError as exc:
        return [f"Invalid SQL expression: {exc}"]

    if not check_columns:
        return errors

    alias_names = {
        alias.alias
        for alias in wrapped.find_all(exp.Alias)
        if isinstance(alias.alias, str) and alias.alias
    }
    scope_by_short: dict[str, list[tuple[str, str, str]]] = {}
    for tkey in scope_tables:
        scope_by_short.setdefault(tkey[2], []).append(tkey)

    for col in wrapped.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        col_name = col.name
        if not col_name or col_name in alias_names:
            continue
        parts = [p.name for p in col.parts if getattr(p, "name", None)]
        if len(parts) >= 4:
            tkeys = schema_index.match_table(parts[-4], parts[-3], parts[-2])
            if not tkeys or not any(col_name in schema_index.tables[k] for k in tkeys):
                errors.append(f"Unknown column '{'.'.join(parts)}' in metric SQL")
        elif len(parts) == 3:
            tkeys = schema_index.match_table(None, parts[0], parts[1])
            if not tkeys or not any(col_name in schema_index.tables[k] for k in tkeys):
                errors.append(f"Unknown column '{'.'.join(parts)}' in metric SQL")
        elif len(parts) == 2:
            tname = parts[0]
            candidates = scope_by_short.get(tname) or schema_index.by_name.get(
                tname, []
            )
            if not candidates:
                errors.append(f"Unknown table '{tname}' referenced in metric SQL")
            elif not any(col_name in schema_index.tables[k] for k in candidates):
                errors.append(f"Column '{col_name}' does not exist on table '{tname}'")
        else:
            candidates = scope_tables or list(schema_index.tables.keys())
            if candidates and not any(
                col_name in schema_index.tables[k] for k in candidates
            ):
                errors.append(
                    f"Column '{col_name}' does not exist on referenced tables"
                )
    return errors


def _validate_metric_entry(
    key: str, entry: dict[str, Any], schema_index: _SchemaIndex, *, check_schema: bool
) -> list[str]:
    errors: list[str] = []
    dialect = entry.get("dialect")
    if dialect not in _DIALECT_MAP:
        return [f"metric '{key}': unsupported dialect '{dialect}'"]

    declared_refs: list[str] = []
    if entry.get("table"):
        declared_refs.append(str(entry["table"]))
    for ref in entry.get("tables") or ():
        declared_refs.append(str(ref))

    scope_tables: list[tuple[str, str, str]] = []
    if check_schema:
        for ref in declared_refs:
            matched = schema_index.resolve_table_ref(ref)
            if not matched:
                errors.append(
                    f"metric '{key}': referenced table '{ref}' does not exist"
                )
            else:
                scope_tables.extend(matched)

    errors.extend(
        f"metric '{key}': {msg}"
        for msg in _validate_sql_fragment(
            str(entry["expression"]),
            dialect=dialect,
            wrap_prefix="SELECT",
            schema_index=schema_index,
            scope_tables=scope_tables,
            check_columns=check_schema,
        )
    )
    for flt in entry.get("filters") or ():
        errors.extend(
            f"metric '{key}' filter: {msg}"
            for msg in _validate_sql_fragment(
                str(flt),
                dialect=dialect,
                wrap_prefix="SELECT 1 WHERE",
                schema_index=schema_index,
                scope_tables=scope_tables,
                check_columns=check_schema,
            )
        )
    return errors


def _validate_relationship_entry(
    key: str, entry: dict[str, Any], schema_index: _SchemaIndex
) -> tuple[list[str], tuple[str, str, str] | None, tuple[str, str, str] | None]:
    errors: list[str] = []
    from_cols = list(entry.get("from_columns") or ())
    to_cols = list(entry.get("to_columns") or ())
    if not from_cols or len(from_cols) != len(to_cols):
        return (
            [
                f"relationship '{key}': from_columns and to_columns must be equal non-empty"
            ],
            None,
            None,
        )

    from_matches = schema_index.match_table(
        entry.get("from_catalog"), entry["from_database"], entry["from_table"]
    )
    to_matches = schema_index.match_table(
        entry.get("to_catalog"), entry["to_database"], entry["to_table"]
    )
    if not from_matches:
        errors.append(
            f"relationship '{key}': source table "
            f"{entry['from_database']}.{entry['from_table']} not found"
        )
        from_key = None
    else:
        from_key = from_matches[0]
        missing_from = [c for c in from_cols if c not in schema_index.tables[from_key]]
        if missing_from:
            errors.append(
                f"relationship '{key}': missing source columns {missing_from}"
            )
    if not to_matches:
        errors.append(
            f"relationship '{key}': target table "
            f"{entry['to_database']}.{entry['to_table']} not found"
        )
        to_key = None
    else:
        to_key = to_matches[0]
        missing_to = [c for c in to_cols if c not in schema_index.tables[to_key]]
        if missing_to:
            errors.append(f"relationship '{key}': missing target columns {missing_to}")
    return errors, from_key, to_key


def _compose_search_description(
    existing: Any, synonyms: list[str] | None, instructions: str | None
) -> str | None:
    parts: list[str] = []
    base = str(existing or "").strip()
    if base:
        parts.append(base)
    if synonyms:
        parts.append("同义词: " + " ".join(synonyms))
    if instructions:
        parts.append("说明: " + instructions)
    return " | ".join(parts) if parts else None


def _load_legacy_sqlite_comments(
    project_id: str,
) -> tuple[dict[str, str], dict[str, str]]:
    table_comments: dict[str, str] = {}
    field_comments: dict[str, str] = {}
    with sessions()() as db:
        rows = (
            db.query(MetadataOverride)
            .filter(MetadataOverride.project_id == project_id)
            .all()
        )
    for row in rows:
        if row.target.startswith("table:"):
            table_comments[row.target[len("table:") :].strip()] = row.comment
        elif row.target.startswith("field:"):
            field_comments[row.target[len("field:") :].strip()] = row.comment
    return table_comments, field_comments


def _resolve_project_source_dir(project_id: str) -> Path | None:
    from . import knowledge, project_context

    try:
        values = (
            knowledge.settings()
            if project_id in (None, "default")
            else knowledge.settings(project_id)
        )
        raw = project_context.from_values(values, project_id).source_directory_value
    except (KeyError, ValueError, RuntimeError, OSError):
        return None
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def _attach_override_source(record: Record) -> None:
    if OVERRIDE_RELATIVE_PATH not in record.source_files:
        record.source_files.append(OVERRIDE_RELATIVE_PATH)


def _match_record_table_keys(
    entry_catalog: str, database: str, name: str, table_records: list[Record]
) -> list[Record]:
    if entry_catalog:
        return [
            r
            for r in table_records
            if _norm_cat(r.data.get("catalog")) == entry_catalog
            and r.data["database"] == database
            and r.data["name"] == name
        ]
    candidates = [
        r
        for r in table_records
        if r.data["database"] == database and r.data["name"] == name
    ]
    empty = [r for r in candidates if not _norm_cat(r.data.get("catalog"))]
    return empty or candidates


def _match_record_field_keys(
    entry_catalog: str,
    database: str,
    table: str,
    name: str,
    field_records: list[Record],
) -> list[Record]:
    if entry_catalog:
        return [
            r
            for r in field_records
            if _norm_cat(r.data.get("catalog")) == entry_catalog
            and r.data["database"] == database
            and r.data["table"] == table
            and r.data["name"] == name
        ]
    candidates = [
        r
        for r in field_records
        if r.data["database"] == database
        and r.data["table"] == table
        and r.data["name"] == name
    ]
    empty = [r for r in candidates if not _norm_cat(r.data.get("catalog"))]
    return empty or candidates


def _legacy_key_variants(catalog: str | None, *parts: str) -> tuple[str, ...]:
    plain = ".".join(parts)
    cat = _norm_cat(catalog)
    return (f"{cat}.{plain}", plain) if cat else (plain,)


def _render_metrics_markdown(metrics: dict[str, dict[str, Any]]) -> str:
    lines = ["# 已确认业务指标（语义覆盖层）", ""]
    for key, metric in metrics.items():
        name = str(metric.get("name") or key)
        lines.append(f"## {name}")
        if metric.get("description"):
            lines.append(f"- 定义：{metric['description']}")
        lines.append(f"- 计算表达式：`{metric['expression']}`")
        if metric.get("dialect"):
            lines.append(f"- 方言：{metric['dialect']}")
        refs = []
        if metric.get("table"):
            refs.append(str(metric["table"]))
        refs.extend(str(t) for t in (metric.get("tables") or ()))
        if refs:
            lines.append("- 关联物理表：" + ", ".join(f"`{r}`" for r in refs))
        if metric.get("filters"):
            lines.append(
                "- 固定过滤条件：" + " AND ".join(f"`{f}`" for f in metric["filters"])
            )
        if metric.get("synonyms"):
            lines.append("- 同义词：" + "、".join(str(s) for s in metric["synonyms"]))
        if metric.get("instructions"):
            lines.append(f"- 使用约束：{metric['instructions']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _apply_entity_overlay(
    rec: Record,
    entry: dict[str, Any],
    *,
    legacy_parts: tuple[str, ...],
    matched_legacy_keys: set[str],
) -> None:
    if "comment" in entry and entry["comment"] is not None:
        rec.data["comment"] = entry["comment"]
    if entry.get("synonyms"):
        rec.data["synonyms"] = list(entry["synonyms"])
    if entry.get("instructions"):
        rec.data["instructions"] = entry["instructions"]
    desc = _compose_search_description(
        rec.data.get("description"),
        entry.get("synonyms"),
        entry.get("instructions"),
    )
    if desc:
        rec.data["description"] = desc
    _attach_override_source(rec)
    for lk in _legacy_key_variants(rec.data.get("catalog"), *legacy_parts):
        matched_legacy_keys.add(lk)


def apply_overrides_detailed(
    records: list[Record],
    project_id: str = "default",
    *,
    source_dir: Path | None = None,
) -> OverrideApplyResult:
    """Overlay Git-tracked structured overrides (and SQLite fallback) onto build records."""
    resolved_dir = source_dir or _resolve_project_source_dir(project_id)
    doc = empty_overrides_doc()
    if resolved_dir is not None:
        doc, _ = load_overrides_file(resolved_dir)

    legacy_tables, legacy_fields = _load_legacy_sqlite_comments(project_id)
    has_yaml = any(doc[s] for s in _SECTION_KEYS)
    if not has_yaml and not legacy_tables and not legacy_fields:
        return OverrideApplyResult(records=records)

    copied = [
        Record(
            r.kind,
            dict(r.data),
            r.source_file,
            r.source_key,
            list(r.source_files),
        )
        for r in records
    ]
    schema_index = _SchemaIndex.from_records(copied)
    table_records = [r for r in copied if r.kind == "table"]
    field_records = [r for r in copied if r.kind == "field"]

    stale_overrides: list[dict[str, str]] = []
    applied_count = 0
    matched_table_legacy_keys: set[str] = set()
    matched_field_legacy_keys: set[str] = set()

    for key, entry in doc["tables"].items():
        cat, db_name, tbl_name = _table_entry_keys(entry)
        targets = _match_record_table_keys(cat, db_name, tbl_name, table_records)
        if not targets:
            stale_overrides.append(
                {
                    "kind": "table",
                    "key": key,
                    "reason": f"Table {db_name}.{tbl_name} not found",
                }
            )
            continue
        for rec in targets:
            _apply_entity_overlay(
                rec,
                entry,
                legacy_parts=(db_name, tbl_name),
                matched_legacy_keys=matched_table_legacy_keys,
            )
            applied_count += 1

    for key, entry in doc["fields"].items():
        cat, db_name, tbl_name, fld_name = _field_entry_keys(entry)
        targets = _match_record_field_keys(
            cat, db_name, tbl_name, fld_name, field_records
        )
        if not targets:
            stale_overrides.append(
                {
                    "kind": "field",
                    "key": key,
                    "reason": f"Field {db_name}.{tbl_name}.{fld_name} not found",
                }
            )
            continue
        for rec in targets:
            _apply_entity_overlay(
                rec,
                entry,
                legacy_parts=(db_name, tbl_name, fld_name),
                matched_legacy_keys=matched_field_legacy_keys,
            )
            applied_count += 1

    # Compatible fallback for any legacy SQLite MetadataOverride not shadowed by YAML
    for rec in table_records:
        for lk in _legacy_key_variants(
            rec.data.get("catalog"), rec.data["database"], rec.data["name"]
        ):
            if lk not in matched_table_legacy_keys and lk in legacy_tables:
                rec.data["comment"] = legacy_tables[lk]
                applied_count += 1
                break
    for rec in field_records:
        for lk in _legacy_key_variants(
            rec.data.get("catalog"),
            rec.data["database"],
            rec.data["table"],
            rec.data["name"],
        ):
            if lk not in matched_field_legacy_keys and lk in legacy_fields:
                rec.data["comment"] = legacy_fields[lk]
                applied_count += 1
                break

    # Overlay explicit RELATES_TO relationships
    rel_index_by_key: dict[tuple[str, str, str, str, str, str, str], int] = {}
    for idx, rec in enumerate(copied):
        if rec.kind == "relationship":
            d = rec.data
            rkey = (
                _norm_cat(d.get("from_catalog")),
                d["from_database"],
                d["from_table"],
                _norm_cat(d.get("to_catalog")),
                d["to_database"],
                d["to_table"],
                d["name"],
            )
            rel_index_by_key[rkey] = idx

    for key, entry in doc["relationships"].items():
        rel_errors, from_key, to_key = _validate_relationship_entry(
            key, entry, schema_index
        )
        if rel_errors or from_key is None or to_key is None:
            stale_overrides.append(
                {
                    "kind": "relationship",
                    "key": key,
                    "reason": rel_errors[0] if rel_errors else "Target table missing",
                }
            )
            continue
        rel_data: dict[str, Any] = {
            "name": entry["name"],
            "from_database": entry["from_database"],
            "from_table": entry["from_table"],
            "from_columns": list(entry["from_columns"]),
            "to_database": entry["to_database"],
            "to_table": entry["to_table"],
            "to_columns": list(entry["to_columns"]),
        }
        if from_key[0]:
            rel_data["from_catalog"] = from_key[0]
        if to_key[0]:
            rel_data["to_catalog"] = to_key[0]
        if entry.get("description"):
            rel_data["description"] = entry["description"]
        if entry.get("instructions"):
            rel_data["instructions"] = entry["instructions"]

        rkey = (
            _norm_cat(rel_data.get("from_catalog")),
            rel_data["from_database"],
            rel_data["from_table"],
            _norm_cat(rel_data.get("to_catalog")),
            rel_data["to_database"],
            rel_data["to_table"],
            rel_data["name"],
        )
        new_rec = Record(
            "relationship",
            rel_data,
            OVERRIDE_RELATIVE_PATH,
            source_key=OVERRIDE_RELATIVE_PATH,
            source_files=[OVERRIDE_RELATIVE_PATH],
        )
        if rkey in rel_index_by_key:
            existing_rec = copied[rel_index_by_key[rkey]]
            new_rec.source_files = sorted(
                {*existing_rec.source_files, OVERRIDE_RELATIVE_PATH}
            )
            copied[rel_index_by_key[rkey]] = new_rec
        else:
            rel_index_by_key[rkey] = len(copied)
            copied.append(new_rec)
        applied_count += 1

    # Overlay confirmed metrics as a searchable document linked to tables
    valid_metrics: dict[str, dict[str, Any]] = {}
    for key, entry in doc["metrics"].items():
        metric_errors = _validate_metric_entry(
            key, entry, schema_index, check_schema=True
        )
        if metric_errors:
            stale_overrides.append(
                {
                    "kind": "metric",
                    "key": key,
                    "reason": metric_errors[0],
                }
            )
            continue
        valid_metrics[key] = entry
        applied_count += 1

    if valid_metrics:
        copied.append(
            Record(
                "document",
                {
                    "category": OVERRIDE_METRICS_CATEGORY,
                    "path": OVERRIDE_METRICS_DOC_PATH,
                    "title": "已确认业务指标（语义覆盖层）",
                    "content": _render_metrics_markdown(valid_metrics),
                    "generated": True,
                },
                OVERRIDE_RELATIVE_PATH,
                source_key=f"overrides/__derived__/{OVERRIDE_METRICS_CATEGORY}/metrics.md",
                source_files=[OVERRIDE_RELATIVE_PATH],
            )
        )

    return OverrideApplyResult(
        records=copied,
        stale_overrides=stale_overrides,
        applied_count=applied_count,
    )


def _current_schema_index(project_id: str) -> tuple[_SchemaIndex | None, str | None]:
    """Build a SchemaIndex from the project's current Neo4j generation when reachable."""
    from . import knowledge
    from .knowledge import StorageUnavailable

    try:
        described = knowledge.describe(summary=False, project_id=project_id)
    except (
        StorageUnavailable,
        KeyError,
        ValueError,
        RuntimeError,
        ConnectionError,
        OSError,
    ) as exc:
        return None, str(exc)
    if not described:
        return _SchemaIndex(), None
    return _SchemaIndex.from_described(described), None


def validate_markdown_relpath(
    raw_path: str,
    *,
    markdown_root: str = "knowledge",
    require_category_subdir: bool = True,
) -> str:
    """Validate and normalize a safe relative Markdown path under ``markdown_root``."""
    value = str(raw_path or "").strip()
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid markdown path")
    if any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value):
        raise ValueError("Invalid markdown path")
    parts_raw = value.split("/")
    if any(p in ("", ".", "..", ".git") for p in parts_raw):
        raise ValueError("Invalid markdown path")
    rel = PurePosixPath(value)
    if rel.is_absolute():
        raise ValueError("Markdown path must be relative")
    parts = rel.parts
    if any(p.startswith(".") for p in parts):
        raise ValueError("Hidden directories or files are not allowed")
    if rel.suffix.lower() != ".md":
        raise ValueError("Markdown document target must end with .md")

    if require_category_subdir:
        root_clean = str(markdown_root or "knowledge").strip().strip("/") or "knowledge"
        root_parts = tuple(p for p in root_clean.split("/") if p)
        if len(parts) < len(root_parts) + 2 or parts[: len(root_parts)] != root_parts:
            raise ValueError(
                f"Document target must live under {root_clean}/<category>/*.md"
            )
        if rel.name.lower() == "index.md":
            raise ValueError(
                "Category index.md cannot be created or overwritten by corrections"
            )
    return rel.as_posix()


def _project_markdown_root(project_id: str) -> str:
    from . import project_context

    try:
        cfg = project_context.plugin_config("source", "markdown", project_id)
    except Exception:  # noqa: BLE001
        return "knowledge"
    if cfg and isinstance(cfg[1], dict):
        raw = str(cfg[1].get("path") or "knowledge").strip().strip("/")
        if raw:
            return raw
    return "knowledge"


def _project_type_safe(project_id: str) -> str:
    from . import project_context

    try:
        return project_context.project_type(project_id)
    except Exception:  # noqa: BLE001
        return project_context.DEFAULT_PROJECT_TYPE


def run_l1_gate(
    *,
    target: str,
    patch: str,
    reason: str,
    structured_patch: dict[str, Any] | None,
    target_kind: str,
    project_id: str,
    resolved_path: str | None = None,
    is_new_file: bool = False,
) -> GateReport:
    """Run deterministic L1 static checks on a correction or structured override proposal."""
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    secret_errors = scan_secrets_and_injections(
        {
            "target": target,
            "patch": patch,
            "reason": reason,
            "structured_patch": structured_patch,
        }
    )
    checks.append(
        {
            "name": "secret_and_injection",
            "passed": not secret_errors,
            "details": secret_errors,
        }
    )
    errors.extend(secret_errors)

    if target_kind == "document":
        path_errors: list[str] = []
        if not str(target or "").startswith("document:"):
            path_errors.append("Document correction target must start with 'document:'")
        else:
            raw_target_path = target[len("document:") :]
            md_root = _project_markdown_root(project_id)
            candidate_path = resolved_path or raw_target_path
            if is_new_file or raw_target_path.startswith(f"{md_root}/"):
                try:
                    validate_markdown_relpath(
                        candidate_path,
                        markdown_root=md_root,
                        require_category_subdir=True,
                    )
                except ValueError as exc:
                    path_errors.append(str(exc))
            elif (
                ".." in raw_target_path.split("/")
                or "\\" in raw_target_path
                or "\x00" in raw_target_path
                or raw_target_path.startswith("/")
            ):
                path_errors.append("Invalid document target path")
            elif resolved_path is not None:
                try:
                    validate_markdown_relpath(
                        resolved_path,
                        markdown_root=md_root,
                        require_category_subdir=False,
                    )
                except ValueError as exc:
                    path_errors.append(str(exc))
        checks.append(
            {
                "name": "markdown_path",
                "passed": not path_errors,
                "details": path_errors,
            }
        )
        errors.extend(path_errors)

    effective_patch: dict[str, Any] | None = structured_patch
    if effective_patch is None and target_kind in {"override", "ossie"}:
        effective_patch = legacy_target_to_patch(target, patch)

    if (
        target_kind in {"override", "ossie"}
        and _project_type_safe(project_id) == "document"
    ):
        pt_err = "Document project does not support warehouse semantic overrides"
        checks.append({"name": "project_type", "passed": False, "details": [pt_err]})
        errors.append(pt_err)

    schema_errors: list[str] = []
    normalized_patch: dict[str, Any] | None = None
    if effective_patch is not None:
        try:
            normalized_patch = normalize_structured_patch(effective_patch)
        except ValueError as exc:
            schema_errors.append(str(exc))
        checks.append(
            {
                "name": "json_schema",
                "passed": not schema_errors,
                "details": schema_errors,
            }
        )
        errors.extend(schema_errors)

    # Physical existence + sqlglot check
    if normalized_patch is not None:
        schema_index, graph_err = _current_schema_index(project_id)
        if schema_index is None or not schema_index.tables:
            # Structured patches require target physical tables/columns in current generation
            if structured_patch is not None:
                msg = (
                    f"Current generation schema unavailable for target verification: {graph_err}"
                    if graph_err
                    else "Current generation has no built physical tables to verify override targets"
                )
                checks.append(
                    {"name": "physical_schema", "passed": False, "details": [msg]}
                )
                errors.append(msg)
                # Still run sqlglot syntax validation even if schema wasn't built
                sql_errors: list[str] = []
                for mkey, mentry in normalized_patch["metrics"].items():
                    sql_errors.extend(
                        _validate_metric_entry(
                            mkey, mentry, _SchemaIndex(), check_schema=False
                        )
                    )
                checks.append(
                    {"name": "sqlglot", "passed": not sql_errors, "details": sql_errors}
                )
                errors.extend(sql_errors)
            else:
                # Legacy table:/field: without Neo4j generation built preserves compat
                checks.append(
                    {
                        "name": "physical_schema",
                        "passed": True,
                        "details": ["skipped: no active graph generation"],
                    }
                )
        else:
            phys_errors: list[str] = _write_ambiguities(normalized_patch, schema_index)
            sql_errors = []
            for tkey, tentry in normalized_patch["tables"].items():
                if not schema_index.match_table(
                    tentry.get("catalog"), tentry["database"], tentry["name"]
                ):
                    phys_errors.append(
                        f"table '{tkey}': {tentry['database']}.{tentry['name']} "
                        "does not exist in current generation"
                    )
            for fkey, fentry in normalized_patch["fields"].items():
                tmatches = schema_index.match_table(
                    fentry.get("catalog"), fentry["database"], fentry["table"]
                )
                if not tmatches or not any(
                    fentry["name"] in schema_index.tables[tm] for tm in tmatches
                ):
                    phys_errors.append(
                        f"field '{fkey}': "
                        f"{fentry['database']}.{fentry['table']}.{fentry['name']} "
                        "does not exist in current generation"
                    )
            for rkey, rentry in normalized_patch["relationships"].items():
                rerrs, _, _ = _validate_relationship_entry(rkey, rentry, schema_index)
                phys_errors.extend(rerrs)
            for mkey, mentry in normalized_patch["metrics"].items():
                merrs = _validate_metric_entry(
                    mkey, mentry, schema_index, check_schema=True
                )
                # Separate SQL syntax/statement failures cleanly alongside schema check
                for err_msg in merrs:
                    if "SQL" in err_msg or "dialect" in err_msg:
                        sql_errors.append(err_msg)
                    else:
                        phys_errors.append(err_msg)
            checks.append(
                {
                    "name": "physical_schema",
                    "passed": not phys_errors,
                    "details": phys_errors,
                }
            )
            checks.append(
                {"name": "sqlglot", "passed": not sql_errors, "details": sql_errors}
            )
            errors.extend(phys_errors)
            errors.extend(sql_errors)

    return GateReport(passed=not errors, checks=checks, errors=errors)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write UTF-8 text via a same-directory temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".semantic-override-")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _legacy_rows_to_patch(rows: list[MetadataOverride]) -> dict[str, Any] | None:
    patch: dict[str, Any] = {"version": 1, "tables": {}, "fields": {}}
    for row in rows:
        converted = legacy_target_to_patch(row.target, row.comment)
        if converted is None:
            continue
        for section in ("tables", "fields"):
            for k, v in converted.get(section, {}).items():
                patch[section][k] = v
    if not patch["tables"] and not patch["fields"]:
        return None
    return patch


def migrate_legacy_metadata_overrides(project_id: str | None = None) -> str | None:
    """Idempotently migrate legacy SQLite ``metadata_overrides`` rows into Git YAML.

    When a project has an initialized ``source_directory``, legacy rows that do
    not already exist in ``overrides/semantic_overrides.yaml`` are merged in (with
    existing YAML entries winning on conflicts), committed in a single Git commit,
    and removed from SQLite so future reverts cannot resurrect stale DB overrides.
    """
    from . import source_directory

    if project_id is None:
        with sessions()() as db:
            project_ids = [
                pid for (pid,) in db.query(MetadataOverride.project_id).distinct().all()
            ]
        last_commit: str | None = None
        for pid in project_ids:
            commit = migrate_legacy_metadata_overrides(pid)
            if commit is not None:
                last_commit = commit
        return last_commit

    src_dir = _resolve_project_source_dir(project_id)
    if src_dir is None or not (src_dir / ".git").exists():
        return None

    with sessions()() as db:
        rows = (
            db.query(MetadataOverride)
            .filter(MetadataOverride.project_id == project_id)
            .order_by(MetadataOverride.target.asc())
            .all()
        )
    if not rows:
        return None

    with source_directory.repository(src_dir) as root:
        if source_directory.git(root, "diff", "--cached", "--name-only").strip():
            logger.warning(
                "Skipping legacy override migration for project %s due to staged git changes",
                project_id,
            )
            return None

        existing_doc, _ = load_overrides_file(root)
        existing_table_keys = {
            _table_entry_keys(entry) for entry in existing_doc["tables"].values()
        }
        existing_field_keys = {
            _field_entry_keys(entry) for entry in existing_doc["fields"].values()
        }

        merged = {
            "version": 1,
            "tables": {k: dict(v) for k, v in existing_doc["tables"].items()},
            "fields": {k: dict(v) for k, v in existing_doc["fields"].items()},
            "relationships": {
                k: dict(v) for k, v in existing_doc["relationships"].items()
            },
            "metrics": {k: dict(v) for k, v in existing_doc["metrics"].items()},
        }
        added = False
        migrated_targets: list[str] = []
        for row in rows:
            converted = legacy_target_to_patch(row.target, row.comment)
            if converted is None:
                continue
            migrated_targets.append(row.target)
            for k, entry in converted.get("tables", {}).items():
                tkey = _table_entry_keys(entry)
                if k not in merged["tables"] and tkey not in existing_table_keys:
                    merged["tables"][k] = entry
                    existing_table_keys.add(tkey)
                    added = True
            for k, entry in converted.get("fields", {}).items():
                fkey = _field_entry_keys(entry)
                if k not in merged["fields"] and fkey not in existing_field_keys:
                    merged["fields"][k] = entry
                    existing_field_keys.add(fkey)
                    added = True

        commit_sha: str | None = None
        if added:
            yaml_path = override_file_path(root)
            _atomic_write_text(yaml_path, dump_overrides_yaml(merged))
            source_directory.git(root, "add", OVERRIDE_RELATIVE_PATH)
            source_directory.git(
                root,
                "commit",
                "-m",
                "迁移历史语义覆盖至 overrides/semantic_overrides.yaml",
                author=LEGACY_MIGRATION_AUTHOR,
            )
            commit_sha = (
                source_directory.git(root, "rev-parse", "HEAD").decode("utf-8").strip()
            )

    if migrated_targets:
        with sessions().begin() as db:
            db.query(MetadataOverride).filter(
                MetadataOverride.project_id == project_id,
                MetadataOverride.target.in_(migrated_targets),
            ).delete(synchronize_session=False)
    return commit_sha
