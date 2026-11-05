"""Shared helpers for real-data DTB sample and lineage comparison scripts."""

import json
import re
from pathlib import Path


def normalize_table(value):
    """Return a lower-case database.table name, or None when it is not a table."""
    text = str(value or "").strip().strip(";").replace("`", "").replace('"', "")
    text = text.replace("'", "")
    parts = [part.strip() for part in text.split(".") if part.strip()]
    if len(parts) < 2 or parts[-2].lower() in {"default", "<default>"}:
        return None
    return f"{parts[-2]}.{parts[-1]}".lower()


def doc_table_name(value):
    """Extract database.table from a Documentation file named after a table."""
    match = re.fullmatch(r"([^. /]+)\.([^. /]+)\.md", Path(value).name, re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}".lower()


def read_jsonl(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
