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
        key = (
            item.get("catalog") or "",
            item["database"],
            item["table"],
            item["field"],
            bool(item.get("_legacy_ref")),
            bool(item.get("_ambiguous_catalog")),
        )
        groups[key].append(item["value"])
        if item.get("row_id") is not None:
            rows[
                (
                    item.get("catalog") or "",
                    item["database"],
                    item["table"],
                    item["row_id"],
                )
            ][item["field"]] = item["value"]
    aliases = defaultdict(set)
    for (catalog, database, table, _), cells in rows.items():
        for field, value in cells.items():
            # Only explicit same-row *_type / *_code / *_id and *_name pairs.
            for suffix in ("_type", "_code", "_id"):
                if field.endswith(suffix):
                    name = cells.get(field[: -len(suffix)] + "_name")
                    if name:
                        aliases[(catalog, database, table, field, value)].add(
                            name.strip()
                        )
    values, statuses, skipped = [], [], 0
    for (catalog, database, table, field, legacy_ref, ambiguous), raw in groups.items():
        distinct = sorted(set(raw))
        normalized = {value.strip() for value in distinct}
        too_many = len(normalized) > limit
        if not ambiguous:
            statuses.append(
                {
                    **({"catalog": catalog} if catalog else {}),
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
                    for x in (
                        (catalog, database, table, field, value)
                        if catalog and not legacy_ref
                        else (database, table, field, value)
                    )
                )
                if (
                    len(ref.encode("utf-8")) > MAX_REF_BYTES
                    or len(value) > MAX_VALUE_CHARS
                ):
                    skipped += 1
                    continue
                values.append(
                    {
                        **({"catalog": catalog} if catalog else {}),
                        **({"ambiguous_catalog": True} if ambiguous else {}),
                        "database": database,
                        "table": table,
                        "field": field,
                        "value": value,
                        "normalized": value.strip(),
                        "aliases": sorted(
                            aliases[(catalog, database, table, field, value)]
                        ),
                        "ref": ref,
                    }
                )
    return values, statuses, skipped


def write(records, generation, storage):
    from . import project_context

    limit = project_context.from_values(storage._settings).dimension_value_limit_int
    values, statuses, skipped = _prepare(records, limit)
    changed_fields = {
        (
            r.data.get("catalog") or "",
            r.data["database"],
            r.data["table"],
            r.data["name"],
        )
        for r in records
        if r.kind == "field"
    }
    if storage.is_incremental and changed_fields:
        _, all_statuses, _ = _prepare(storage.all_records, limit)
        statuses = [
            s
            for s in statuses
            if (s.get("catalog") or "", s["database"], s["table"], s["field"])
            not in changed_fields
        ] + [
            s
            for s in all_statuses
            if (s.get("catalog") or "", s["database"], s["table"], s["field"])
            in changed_fields
        ]
    sources = defaultdict(set)
    primary_sources = defaultdict(set)
    for record in records:
        if record.kind == "dimension_value":
            item = record.data
            table = (item.get("catalog") or "", item["database"], item["table"])
            sources[table].add(record.source_file)
            primary_sources[
                (
                    item.get("catalog") or "",
                    item["database"],
                    item["table"],
                    item["field"],
                    item["value"],
                )
            ].add(record.source_file)
    for value in values:
        key = (
            value.get("catalog") or "",
            value["database"],
            value["table"],
            value["field"],
            value["value"],
        )
        value["source_files"] = sorted(sources[key[:3]])
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
            "UNWIND $rows AS row MATCH (f:SDAField {generation:$generation, database:row.database, table:row.table, name:row.field}) WHERE coalesce(f.catalog, '') = coalesce(row.catalog, '') SET f.dimension_count = row.count, f.dimension_too_many = row.too_many, f.dimension_samples = row.samples",
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
    if hasattr(storage, "rebuilt_dim_tables"):
        storage.rebuilt_dim_tables.update(
            (row.get("catalog") or "", row["database"], row["table"]) for row in values
        )
        storage.rebuilt_dim_tables.update(
            (row.get("catalog") or "", row["database"], row["table"])
            for row in statuses
        )
    return StageResult(
        counts={"dimension_value": len(values), "dimension_value_skipped": skipped}
    )
