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


def check_incremental_catalog_isolation(ctx):
    import json
    import time

    from neo4j import GraphDatabase

    path = ctx.source_dir / "dimension_values.json"
    original = path.read_bytes()
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )

    def snapshot():
        with driver.session() as session:
            return [
                dict(row)
                for row in session.run(
                    "MATCH (v:SDADimensionValue {generation:$g, database:'i337shop'}) "
                    "RETURN v.catalog AS catalog, v.value AS value, v.ref AS ref, v.build_id AS build_id, elementId(v) AS id ORDER BY catalog",
                    g=ctx.generation,
                )
            ]

    def build():
        response = ctx.rest.post("/api/v1/builds", json={"build_type": "incremental"})
        response.raise_for_status()
        build_id = response.json()["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = ctx.rest.get(f"/api/v1/builds/{build_id}")
            response.raise_for_status()
            result = response.json()
            if result["status"] != "running" and result["status"] != "queued":
                ctx.require(
                    result["status"] == "succeeded"
                    and result["build_type"] == "incremental",
                    f"incremental build failed or fell back: {result}",
                )
                return
            time.sleep(1)
        raise AssertionError("incremental build timed out")

    try:
        before = snapshot()
        other = next(row for row in before if row["catalog"] == "internal")
        rows = json.loads(original)
        hive = next(row for row in rows if row["catalog"] == "hive")
        hive["value"] = "hive_changed"
        path.write_text(json.dumps(rows), encoding="utf-8")
        build()
        changed = snapshot()
        ctx.require(
            next(row for row in changed if row["catalog"] == "internal") == other,
            f"other catalog was rebuilt: {before} -> {changed}",
        )
        ctx.require(
            any(row["value"] == "hive_changed" for row in changed),
            f"changed catalog missing: {changed}",
        )
        rows.remove(hive)
        path.write_text(json.dumps(rows), encoding="utf-8")
        build()
        deleted = snapshot()
        ctx.require(deleted == [other], f"deletion crossed catalog: {deleted}")
        hits = ctx.mcp_call(
            "lookup_dim_values",
            {"table": "internal.i337shop.orders", "keyword": "internal_only"},
        )
        ctx.require(
            [row["value"] for row in hits] == ["internal_only"],
            f"other catalog lost after deletion: {hits}",
        )
        return {"before": before, "changed": changed, "deleted": deleted}
    finally:
        path.write_bytes(original)
        driver.close()


def check_legacy_snapshot_incremental(ctx):
    import json
    import time

    path = ctx.source_dir / "dimension_values.json"
    original = path.read_bytes()
    try:
        hits = ctx.mcp_call(
            "lookup_dim_values", {"table": "legacy.orders", "keyword": "legacy_old"}
        )
        ctx.require(
            [row["ref"] for row in hits]
            == ["dimension_value:legacy.orders.code.legacy_old"],
            f"legacy full build changed: {hits}",
        )
        rows = json.loads(original)
        next(row for row in rows if row["database"] == "legacy")["value"] = "legacy_new"
        path.write_text(json.dumps(rows), encoding="utf-8")
        response = ctx.rest.post("/api/v1/builds", json={"build_type": "incremental"})
        response.raise_for_status()
        build_id = response.json()["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            build = ctx.rest.get(f"/api/v1/builds/{build_id}").json()
            if build["status"] not in ("running", "queued"):
                ctx.require(
                    build["status"] == "succeeded"
                    and build["build_type"] == "incremental",
                    f"legacy incremental failed: {build}",
                )
                break
            time.sleep(1)
        else:
            raise AssertionError("legacy incremental timed out")
        hits = ctx.mcp_call(
            "lookup_dim_values", {"table": "legacy.orders", "keyword": "legacy_new"}
        )
        ctx.require(
            [row["ref"] for row in hits]
            == ["dimension_value:legacy.orders.code.legacy_new"],
            f"legacy incremental changed: {hits}",
        )
        return {"hits": hits}
    finally:
        path.write_bytes(original)
