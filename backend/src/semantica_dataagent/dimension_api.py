"""Lookup indexed dimension values by value or a paired name."""

import re

from . import knowledge, project_context
from .mcp_contract import ToolInvalidInput


def lookup_dim_values(
    table: str,
    keyword: str,
    field: str | None = None,
    project_id: str | None = None,
    catalog: str | None = None,
) -> list[dict]:
    keyword = keyword.strip()
    if not table or not keyword:
        raise ValueError("table and keyword are required")
    values = (
        knowledge.settings()
        if project_id in (None, project_context.DEFAULT_PROJECT_ID)
        else knowledge.settings(project_id)
    )
    project = project_context.from_values(values)
    store = knowledge.read_graph(values)
    parts = table.split(".")
    if len(parts) == 3:
        table_catalog, database, table_name = parts
        if catalog is not None and catalog != table_catalog:
            raise ToolInvalidInput("422: catalog conflicts with qualified table")
        catalog = table_catalog
    elif len(parts) == 2:
        database, table_name = parts
    elif len(parts) == 1:
        database, table_name = None, parts[0]
    else:
        raise ToolInvalidInput("422: invalid table name")
    candidates = knowledge.execute(
        store,
        "MATCH (t:SDATable {generation:$generation, name:$table}) "
        "WHERE ($database IS NULL OR t.database = $database) "
        "AND ($catalog IS NULL OR coalesce(t.catalog, '') = $catalog) "
        "RETURN DISTINCT coalesce(t.catalog, '') AS catalog, t.database AS database, t.name AS table",
        {
            "generation": project.generation,
            "table": table_name,
            "database": database,
            "catalog": catalog,
        },
    )
    if len(candidates) > 1:
        names = sorted(
            ".".join(
                part for part in (row["catalog"], row["database"], row["table"]) if part
            )
            for row in candidates
        )
        raise ToolInvalidInput("422: ambiguous table; candidates: " + ", ".join(names))
    if not candidates:
        return []
    selected = candidates[0]
    params = {
        "generation": project.generation,
        "table": selected["table"],
        "database": selected["database"],
        "catalog": selected["catalog"],
        "field": field,
        "keyword": keyword,
        "query": re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", keyword),
    }
    exact = knowledge.execute(
        store,
        "MATCH (v:SDADimensionValue {generation:$generation, database:$database, table:$table}) WHERE NOT coalesce(v.ambiguous_catalog, false) AND coalesce(v.catalog, '') = $catalog AND ($field IS NULL OR v.field = $field) AND (toLower(v.normalized) = toLower($keyword) OR any(alias IN v.aliases WHERE toLower(alias) = toLower($keyword))) RETURN properties(v) AS item",
        params,
    )
    fulltext = knowledge.execute(
        store,
        "CALL db.index.fulltext.queryNodes('"
        + project_context.index_name("dimension_values")
        + "', $query) YIELD node WHERE node.generation = $generation AND node.database = $database AND node.table = $table AND NOT coalesce(node.ambiguous_catalog, false) AND coalesce(node.catalog, '') = $catalog AND ($field IS NULL OR node.field = $field) RETURN properties(node) AS item",
        params,
    )
    result = {}
    for method, hits in (("exact", exact), ("fulltext", fulltext)):
        for hit in hits:
            item = hit["item"]
            result.setdefault(
                item["ref"],
                {
                    "ref": item["ref"],
                    "database": item["database"],
                    "table": item["table"],
                    "field": item["field"],
                    "value": item["value"],
                    "match": method,
                },
            )
    return list(result.values())
