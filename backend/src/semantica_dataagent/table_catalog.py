"""Resolve SQL table identifiers to knowledge metadata without guessing catalogs."""

from sqlglot import exp

SUPPORTED_CATALOGS = frozenset({"internal", "hive", "awsdatacatalog"})


def metadata_catalog(metadata):
    value = metadata.get("catalog")
    return value.strip().lower() if isinstance(value, str) else ""


def metadata_table_parts(metadata):
    database = metadata.get("database")
    name = metadata.get("name")
    return (
        metadata_catalog(metadata),
        database.strip().lower() if isinstance(database, str) else "",
        name.strip().lower() if isinstance(name, str) else "",
    )


def sql_table_parts(table):
    return (
        (table.catalog or "").strip().lower(),
        (table.db or "").strip().lower(),
        (table.name or "").strip().lower(),
    )


def table_lookup(tables):
    lookup = {"catalog": {}, "db": {}, "name": {}}
    for metadata in tables:
        catalog, database, name = metadata_table_parts(metadata)
        if not database or not name:
            continue
        if catalog:
            lookup["catalog"].setdefault((catalog, database, name), []).append(metadata)
        lookup["db"].setdefault((database, name), []).append(metadata)
        lookup["name"].setdefault(name, []).append(metadata)
    return lookup


def resolve_table(table, lookup):
    """Return ``(metadata, reason, candidates)`` for one SQL table.

    Three-part names match catalog metadata exactly.  Knowledge generations
    written before catalogs were recorded have no catalog field; for those,
    the well-known Doris catalogs are accepted only when the database.table
    pair has exactly one such catalog-less match.  A known catalog absent from
    catalog-aware metadata is never guessed, and duplicate or multi-catalog
    matches are reported as ambiguous instead of selecting one.

    When knowledge records a catalog, an unqualified ``database.table`` name is
    rejected with the ``unqualified`` reason instead of being promoted to that
    catalog: the executor would still run the unqualified SQL, which can land
    in the default catalog.  The caller must ask the user for the explicit
    ``catalog.database.table`` form; legacy catalog-less knowledge keeps
    accepting two-part names.
    """
    catalog, database, name = sql_table_parts(table)
    if not name or not isinstance(table.this, exp.Identifier):
        return None, "unsupported", []
    base = (
        lookup["name"].get(name, [])
        if not database
        else lookup["db"].get((database, name), [])
    )
    if catalog:
        exact = lookup["catalog"].get((catalog, database, name), [])
        if len(exact) == 1:
            return exact[0], None, exact
        if len(exact) > 1:
            return None, "ambiguous", exact
        if catalog not in SUPPORTED_CATALOGS:
            return None, "catalog", []
        uncatalogued = [item for item in base if not metadata_catalog(item)]
        if len(base) == 1 and len(uncatalogued) == 1:
            return uncatalogued[0], None, uncatalogued
        if len(uncatalogued) > 1:
            return None, "ambiguous", uncatalogued
        if base:
            return None, "catalog", []
        return None, "missing", []
    if len(base) == 1:
        metadata = base[0]
        if metadata_catalog(metadata):
            return None, "unqualified", base
        return metadata, None, base
    if base:
        return None, "ambiguous", base
    return None, "missing", []


def match_table(table, lookup):
    metadata, reason, _ = resolve_table(table, lookup)
    return metadata, reason


def executable_name(metadata, table):
    """Return the physical table name, preserving the catalog's spelling."""
    catalog = metadata.get("catalog") if metadata else None
    if not isinstance(catalog, str) or not catalog.strip():
        catalog = table.catalog
    _, database, name = metadata_table_parts(metadata or {})
    if not name:
        _, database, name = sql_table_parts(table)
    parts = [
        catalog.strip() if isinstance(catalog, str) else "",
        database,
        name,
    ]
    return ".".join(part for part in parts if part)


def same_physical_table(inner, outer, metadata, lookup):
    matched, _ = match_table(inner, lookup)
    if matched is not metadata:
        return False
    inner_catalog, _, _ = sql_table_parts(inner)
    outer_catalog, _, _ = sql_table_parts(outer)
    return inner_catalog == outer_catalog
