"""Index dimension values; row_id associates code and name cells in the same row."""

from collections import defaultdict
from urllib.parse import quote

from .refs import MAX_REF_BYTES
from .stage_contract import StageResult

WRITE_BATCH_SIZE = 2000
MAX_VALUE_CHARS = 512
MAX_SAMPLE_CHARS = 200


def prepare(records, limit):
    values, statuses, _ = _prepare(records, limit)
    return values, statuses


def _prepare(records, limit):
    groups = defaultdict(list)
    rows = defaultdict(dict)
    for record in records:
        if record.kind != "dimension_value":
            continue
        item = record.data
        key = (item["database"], item["table"], item["field"])
        groups[key].append(item["value"])
        if item.get("row_id") is not None:
            rows[(item["database"], item["table"], item["row_id"])][item["field"]] = (
                item["value"]
            )
    aliases = defaultdict(set)
    for (database, table, _), cells in rows.items():
        for field, value in cells.items():
            # Only explicit same-row *_type / *_code / *_id and *_name pairs.
            for suffix in ("_type", "_code", "_id"):
                if field.endswith(suffix):
                    name = cells.get(field[: -len(suffix)] + "_name")
                    if name:
                        aliases[(database, table, field, value)].add(name.strip())
    values, statuses, skipped = [], [], 0
    for (database, table, field), raw in groups.items():
        distinct = sorted(set(raw))
        normalized = {value.strip() for value in distinct}
        too_many = len(normalized) > limit
        statuses.append(
            {
                "database": database,
                "table": table,
                "field": field,
                "count": len(normalized),
                "too_many": too_many,
                "samples": [
                    value for value in distinct if len(value) <= MAX_SAMPLE_CHARS
                ][:5],
            }
        )
        if not too_many:
            for value in distinct:
                ref = "dimension_value:" + ".".join(
                    quote(x, safe="").replace(".", "%2E")
                    for x in (database, table, field, value)
                )
                if (
                    len(ref.encode("utf-8")) > MAX_REF_BYTES
                    or len(value) > MAX_VALUE_CHARS
                ):
                    skipped += 1
                    continue
                values.append(
                    {
                        "database": database,
                        "table": table,
                        "field": field,
                        "value": value,
                        "normalized": value.strip(),
                        "aliases": sorted(aliases[(database, table, field, value)]),
                        "ref": ref,
                    }
                )
    return values, statuses, skipped


def write(records, generation, storage):
    from . import project_context

    limit = project_context.from_values(storage._settings).dimension_value_limit_int
    values, statuses, skipped = _prepare(records, limit)
    changed_fields = {
        (r.data["database"], r.data["table"], r.data["name"])
        for r in records
        if r.kind == "field"
    }
    if storage.is_incremental and changed_fields:
        _, all_statuses, _ = _prepare(storage.all_records, limit)
        statuses = [
            s
            for s in statuses
            if (s["database"], s["table"], s["field"]) not in changed_fields
        ] + [
            s
            for s in all_statuses
            if (s["database"], s["table"], s["field"]) in changed_fields
        ]
    sources = defaultdict(set)
    primary_sources = defaultdict(set)
    for record in records:
        if record.kind == "dimension_value":
            item = record.data
            table = (item["database"], item["table"])
            sources[table].add(record.source_file)
            primary_sources[
                (item["database"], item["table"], item["field"], item["value"])
            ].add(record.source_file)
    for value in values:
        key = (value["database"], value["table"], value["field"], value["value"])
        value["source_files"] = sorted(sources[key[:2]])
        value["source_file"] = min(primary_sources[key])
        value["build_id"] = storage.build_id
    storage.execute(
        "CREATE FULLTEXT INDEX __DIMENSION_INDEX__ IF NOT EXISTS FOR (n:SDADimensionValue) ON EACH [n.normalized, n.aliases] OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}".replace(
            "__DIMENSION_INDEX__", project_context.index_name("dimension_values")
        ),
        {},
    )
    storage.execute(
        f"CALL db.awaitIndex('{project_context.index_name('dimension_values')}', 120)",
        {},
    )
    for rows, query in (
        (
            statuses,
            "UNWIND $rows AS row MATCH (f:SDAField {generation:$generation, database:row.database, table:row.table, name:row.field}) SET f.dimension_count = row.count, f.dimension_too_many = row.too_many, f.dimension_samples = row.samples",
        ),
        (
            values,
            "UNWIND $rows AS row CREATE (v:SDAEntity:SDADimensionValue) SET v = row, v.generation = $generation",
        ),
    ):
        for offset in range(0, len(rows), WRITE_BATCH_SIZE):
            storage.execute(
                query,
                {
                    "rows": rows[offset : offset + WRITE_BATCH_SIZE],
                    "generation": generation,
                },
            )
    return StageResult(
        counts={"dimension_value": len(values), "dimension_value_skipped": skipped}
    )
