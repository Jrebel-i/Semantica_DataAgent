"""Check catalog-scoped dimension lookup against an isolated two-catalog build."""


def check_dimension_catalog(ctx):
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    try:
        with driver.session() as session:
            fields = [
                dict(row)
                for row in session.run(
                    "MATCH (f:SDAField {generation:$generation, database:'i337shop', table:'orders', name:'code'}) "
                    "OPTIONAL MATCH (f)-[:HAS_VALUE]->(v:SDADimensionValue) "
                    "RETURN f.catalog AS catalog, f.dimension_count AS count, f.dimension_samples AS samples, collect(v.value) AS values ORDER BY catalog",
                    generation=ctx.generation,
                )
            ]
        expected = [
            {
                "catalog": "hive",
                "count": 1,
                "samples": ["hive_only"],
                "values": ["hive_only"],
            },
            {
                "catalog": "internal",
                "count": 1,
                "samples": ["internal_only"],
                "values": ["internal_only"],
            },
        ]
        ctx.require(
            fields == expected, f"catalog groups or relationships mixed: {fields}"
        )
        for catalog in ("hive", "internal"):
            for value_catalog in ("hive", "internal"):
                hits = ctx.mcp_call(
                    "lookup_dim_values",
                    {
                        "table": f"{catalog}.i337shop.orders",
                        "field": "code",
                        "keyword": f"{value_catalog}_only",
                    },
                )
                expected_values = (
                    [f"{catalog}_only"] if catalog == value_catalog else []
                )
                ctx.require(
                    [hit["value"] for hit in hits] == expected_values,
                    f"wrong catalog results: {catalog} {value_catalog} {hits}",
                )
        hits = ctx.mcp_call(
            "lookup_dim_values",
            {"table": "i337shop.orders", "catalog": "hive", "keyword": "hive_only"},
        )
        ctx.require(
            [hit["value"] for hit in hits] == ["hive_only"],
            f"catalog parameter ignored: {hits}",
        )
        result = ctx.mcp.call_tool(
            "lookup_dim_values", {"table": "i337shop.orders", "keyword": "hive_only"}
        )
        message = " ".join(getattr(item, "text", "") for item in result.content)
        ctx.require(
            result.isError
            and "422" in message
            and "hive.i337shop.orders" in message
            and "internal.i337shop.orders" in message,
            f"missing ambiguity error: {result}",
        )
        return {"fields": fields}
    finally:
        driver.close()
