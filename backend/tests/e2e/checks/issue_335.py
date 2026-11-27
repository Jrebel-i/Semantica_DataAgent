"""Validate derived projections and JOIN evidence through MCP."""


def check_derived_star(ctx):
    base = (
        "WITH c1 AS (SELECT * FROM shop.orders WHERE dt = '2025-01-01'), "
        "c2 AS (SELECT c1.*, COUNT(*) AS cnt FROM c1) "
    )
    good = ctx.mcp_call("validate_sql", {"sql": base + "SELECT c2.id, c2.cnt FROM c2"})
    ctx.require(good["status"] == "pass", f"nested star: {good}")
    bad = ctx.mcp_call("validate_sql", {"sql": base + "SELECT c2.nope FROM c2"})
    ctx.require(
        any(f["name"] == "c2.nope" and f["status"] == "error" for f in bad["fields"]),
        f"missing derived field: {bad}",
    )
    unknown = ctx.mcp_call(
        "validate_sql", {"sql": "SELECT x.nope FROM (SELECT * FROM mystery) x"}
    )
    ctx.require(
        any(
            f["name"] == "x.nope" and f["status"] == "unknown"
            for f in unknown["fields"]
        ),
        f"unknown source: {unknown}",
    )


def check_join_using(ctx):
    sql = (
        "SELECT o.id FROM shop.orders o JOIN shop.daily_sales d USING (id) "
        "WHERE o.dt = '2025-01-01' AND d.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result["status"] == "pass", f"USING with lineage: {result}")


def check_ambiguous_using(ctx):
    sql = (
        "SELECT o.id FROM shop.orders o JOIN shop.daily_sales d USING (id) "
        "JOIN shop.customers c USING (id) WHERE o.dt = '2025-01-01' "
        "AND d.dt = '2025-01-01' AND c.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(
        len(result["joins"]) == 2 and result["joins"][1]["status"] != "supported",
        f"ambiguous USING was accepted: {result}",
    )


def check_duplicate_alias_returns_issue(ctx):
    sql = (
        "SELECT a.id FROM shop.orders a JOIN shop.customers a ON a.id = a.id "
        "WHERE a.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(
        result.get("status") == "issues" and bool(result.get("issues")),
        f"duplicate alias must produce a structured issue: {result}",
    )


def check_cte_star_column_aliases(ctx):
    sql = (
        "WITH c(customer_key, created_time, partition_day) AS "
        "(SELECT * FROM shop.customers WHERE dt = '2025-01-01') "
        "SELECT c.customer_key FROM c"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result.get("status") == "pass", f"CTE column aliases: {result}")


def check_duplicate_projected_fields_are_ambiguous(ctx):
    sql = (
        "SELECT x.id FROM (SELECT o.*, d.* FROM shop.orders o "
        "JOIN shop.daily_sales d ON o.id = d.id "
        "WHERE o.dt = '2025-01-01' AND d.dt = '2025-01-01') x"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(
        any(
            f.get("name") == "x.id" and f.get("status") == "ambiguous"
            for f in result.get("fields", [])
        ),
        f"duplicate projected field must be ambiguous: {result}",
    )


def check_using_merged_field_is_visible(ctx):
    sql = (
        "SELECT x.id FROM (SELECT * FROM shop.orders o "
        "JOIN shop.daily_sales d USING (id) "
        "WHERE o.dt = '2025-01-01' AND d.dt = '2025-01-01') x"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result.get("status") == "pass", f"merged USING field: {result}")


def check_derived_star_join_uses_source_evidence(ctx):
    sql = (
        "SELECT o.id FROM shop.orders o JOIN "
        "(SELECT * FROM shop.daily_sales WHERE dt = '2025-01-01') d USING (id) "
        "WHERE o.dt = '2025-01-01'"
    )
    result = ctx.mcp_call("validate_sql", {"sql": sql})
    ctx.require(result.get("status") == "pass", f"derived USING: {result}")


def check_join_pair_evidence_from_graph(ctx):
    import sqlite3
    from uuid import uuid4

    from neo4j import GraphDatabase

    prefix = "i335_" + uuid4().hex[:12]
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, ctx.runtime.password)
    )
    generation = prefix + "_generation"
    original_generation = ctx.generation
    switched = False
    names = {key: prefix + "_" + key for key in ("a", "b", "c", "d")}
    try:
        with driver.session() as session:
            session.run(
                """
                UNWIND $tables AS item
                CREATE (t:SDAEntity:SDATable {generation:$g, database:$db, name:item.name, ref:item.ref})
                WITH t, item
                UNWIND ['good','wrong'] AS name
                CREATE (f:SDAEntity:SDAField {generation:$g, database:$db, name:name, partition:false})
                CREATE (t)-[:HAS_FIELD]->(f)
                """,
                g=generation,
                db=prefix,
                tables=[
                    {"name": name, "ref": f"table:{prefix}.{name}"}
                    for name in names.values()
                ],
            ).consume()
            session.run(
                """
                MATCH (a:SDATable {generation:$g, database:$db, name:$a})
                MATCH (b:SDATable {generation:$g, database:$db, name:$b})
                CREATE (a)-[:RELATES_TO {name:'good_relation', from_table:$aname,
                    to_table:$bname, from_columns:['good'], to_columns:['good']}]->(b)
                CREATE (:SDAEntity:SDAChunk {generation:$g, database:$db, ref:$docref, content:$content})
                """,
                g=generation,
                db=prefix,
                a=names["a"],
                b=names["b"],
                aname=f"{prefix}.{names['a']}",
                bname=f"{prefix}.{names['b']}",
                docref=f"document:{prefix}",
                content=f"{prefix}.{names['c']}.good = {prefix}.{names['d']}.good",
            ).consume()
        with sqlite3.connect(ctx.runtime.db_path) as db:
            db.execute(
                "INSERT INTO knowledge_generations (name, project_id, deleting) VALUES (?, 'default', 0)",
                (generation,),
            )
        response = ctx.rest.post(f"/api/v1/generations/{generation}/switch")
        ctx.require(
            response.status_code == 200, f"switch fixture generation: {response.text}"
        )
        switched = True
        relation = f"SELECT a.good FROM {prefix}.{names['a']} a JOIN {prefix}.{names['b']} b ON "
        document = f"SELECT a.good FROM {prefix}.{names['c']} a JOIN {prefix}.{names['d']} b ON "
        for label, base in (("relationship", relation), ("document", document)):
            good = ctx.mcp_call("validate_sql", {"sql": base + "a.good = b.good"})
            ctx.require(good.get("status") == "pass", f"{label} fixture: {good}")
            mixed = ctx.mcp_call(
                "validate_sql", {"sql": base + "a.good = b.good AND a.wrong = b.wrong"}
            )
            ctx.require(
                any(
                    i.get("kind") == "join" and "a.wrong" in i.get("message", "")
                    for i in mixed.get("issues", [])
                ),
                f"{label} mixed keys accepted: {mixed}",
            )
    finally:
        if switched:
            response = ctx.rest.post(
                f"/api/v1/generations/{original_generation}/switch"
            )
            ctx.require(
                response.status_code == 200, f"restore generation: {response.text}"
            )
        with sqlite3.connect(ctx.runtime.db_path) as db:
            db.execute(
                "DELETE FROM knowledge_generations WHERE name = ?", (generation,)
            )
        with driver.session() as session:
            session.run(
                "MATCH (n:SDAEntity {generation:$g, database:$db}) DETACH DELETE n",
                g=generation,
                db=prefix,
            ).consume()
        driver.close()
