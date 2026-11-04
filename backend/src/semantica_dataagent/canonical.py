"""Read version 1 canonical records from files named after their entity type."""

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from jsonschema import ValidationError, validate
from openpyxl import load_workbook

KINDS = {
    "tables": "table",
    "fields": "field",
    "tasks": "task",
    "lineage": "lineage",
    "dimension_values": "dimension_value",
    "documents": "document",
}
SCHEMAS = Path(__file__).resolve().parent / "schemas" / "v1"
if not SCHEMAS.exists():
    SCHEMAS = Path(__file__).resolve().parents[3] / "schemas" / "v1"


@dataclass(frozen=True)
class Record:
    kind: str
    data: dict
    source_file: str | None = None
    source_key: str | None = None
    source_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadResult:
    records: list[Record]
    hashes: dict[str, str]
    stats: dict[str, int] = field(default_factory=dict)


class CanonicalAdapter:
    id = "canonical"
    version = "1.0.0"
    display_name = "Canonical"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
    }

    def read(self, directory: Path, parameters: dict) -> ReadResult:
        validate(parameters, self.parameters_schema)
        if not directory.is_dir():
            raise ValueError(f"Source directory does not exist: {directory}")
        records = []
        hashes = {}
        for path in sorted(directory.rglob("*")):
            if ".git" in path.relative_to(directory).parts:
                continue
            if not path.is_file() or path.suffix.lower() not in {
                ".json",
                ".csv",
                ".xlsx",
            }:
                continue
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Canonical file escapes source directory")
            kind = KINDS.get(path.stem)
            if kind is None:
                raise ValueError(f"Unknown entity file: {path}")
            raw = path.read_bytes()
            hashes[str(path.relative_to(directory))] = hashlib.sha256(raw).hexdigest()
            try:
                if path.suffix.lower() == ".json":
                    rows = json.loads(raw)
                    if not isinstance(rows, list):
                        raise ValueError("Expected an array")
                elif path.suffix.lower() == ".csv":
                    rows = list(
                        csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
                    )
                else:
                    book = load_workbook(
                        io.BytesIO(raw), read_only=True, data_only=True
                    )
                    try:
                        data = iter(book.active.values)
                        headers = next(data)
                        rows = [dict(zip(headers, row, strict=True)) for row in data]
                    finally:
                        book.close()
                schema = json.loads((SCHEMAS / f"{kind}.schema.json").read_text())
                for row in rows:
                    if kind == "field" and isinstance(row.get("partition"), str):
                        value = row["partition"].lower()
                        if value in {"true", "false"}:
                            row["partition"] = value == "true"
                    validate(row, schema)
                    records.append(
                        Record(kind, row, path.relative_to(directory).as_posix())
                    )
            except (ValueError, ValidationError, TypeError) as exc:
                raise ValueError(f"{path}: {exc}") from exc
        return ReadResult(records, hashes)
