"""Lookup indexed dimension values by value or a paired name."""

import re

from . import knowledge, project_context


def lookup_dim_values(table: str, keyword: str, field: str | None = None) -> list[dict]:
    keyword = keyword.strip()
    if not table or not keyword:
        raise ValueError("table and keyword are required")
    values = knowledge.settings()
    project = project_context.from_values(values)
    store = knowledge.read_graph(values)
    params = {
        "generation": project.generation,
        "table": table,
        "field": field,
        "keyword": keyword,
        "query": re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", keyword),
    }
    exact = knowledge.execute(
        store,
        "MATCH (v:SDADimensionValue {generation:$generation}) WHERE (v.table = $table OR v.database + '.' + v.table = $table) AND ($field IS NULL OR v.field = $field) AND (toLower(v.normalized) = toLower($keyword) OR any(alias IN v.aliases WHERE toLower(alias) = toLower($keyword))) RETURN properties(v) AS item",
        params,
    )
    fulltext = knowledge.execute(
        store,
        "CALL db.index.fulltext.queryNodes('"
        + project_context.index_name("dimension_values")
        + "', $query) YIELD node WHERE node.generation = $generation AND (node.table = $table OR node.database + '.' + node.table = $table) AND ($field IS NULL OR node.field = $field) RETURN properties(node) AS item",
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
