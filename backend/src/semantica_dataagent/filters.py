"""Runtime inclusion and exclusion rules for canonical records."""

import fnmatch
import json

from .db import Setting, sessions

DEFAULT_FILTERS = {
    "database_include": [],
    "database_exclude": ["tmp", "test"],
    "task_include": [],
    "task_exclude": [],
}


def get_filters():
    with sessions()() as db:
        row = db.get(Setting, "ingestion_filters")
        return json.loads(row.value) if row else DEFAULT_FILTERS.copy()


def filter_records(records, rules):
    def allowed(value, include, exclude):
        return (
            not include
            or any(fnmatch.fnmatchcase(value, pattern) for pattern in include)
        ) and not any(fnmatch.fnmatchcase(value, pattern) for pattern in exclude)

    kept = []
    for record in records:
        data = record.data
        ok = True
        if "database" in data:
            ok = allowed(
                data["database"], rules["database_include"], rules["database_exclude"]
            )
        if record.kind == "task":
            ok = ok and allowed(
                data["name"], rules["task_include"], rules["task_exclude"]
            )
        if ok:
            kept.append(record)
    return kept, len(records) - len(kept)
