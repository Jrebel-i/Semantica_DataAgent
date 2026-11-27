"""Verify the Hive/Spark syntax-only dialect notice through the MCP server."""

NOTICE = "该方言仅用于语法检查，交付请使用 Doris SQL。"
SQL = "SELECT id FROM shop.orders WHERE dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"


def check_dialect_notice_paths(ctx):
    outcomes = {}
    for label, sql, expected_status in (
        ("pass", SQL, "pass"),
        ("issues", "SELECT id FROM shop.orders", "issues"),
    ):
        calls = {
            "omitted": ctx.mcp_call("validate_sql", {"sql": sql}),
            "hive": ctx.mcp_call("validate_sql", {"sql": sql, "dialect": "hive"}),
            "spark": ctx.mcp_call("validate_sql", {"sql": sql, "dialect": "spark"}),
            "doris": ctx.mcp_call("validate_sql", {"sql": sql, "dialect": "doris"}),
        }
        for name in ("omitted", "hive", "spark"):
            ctx.require(
                calls[name].get("dialect_notice") == NOTICE,
                f"{label}/{name}: expected the syntax-only notice, got {calls[name]}",
            )
        ctx.require(
            "dialect_notice" not in calls["doris"],
            f"{label}/doris: unexpected notice in {calls['doris']}",
        )
        ctx.require(
            calls["omitted"] == calls["hive"],
            f"{label}: omitted dialect must resolve to the hive default",
        )
        for name, result in calls.items():
            ctx.require(
                result.get("status") == expected_status,
                f"{label}/{name}: expected {expected_status}, got {result}",
            )
            ctx.require(
                result.get("issues") == calls["doris"].get("issues"),
                f"{label}/{name}: the notice changed issues: {result}",
            )
        outcomes[label] = {name: result.get("status") for name, result in calls.items()}
    return outcomes


def check_date_expressions_match_across_dialects(ctx):
    checks = {
        "DATE_SUB": (
            "SELECT id FROM shop.orders "
            "WHERE dt = DATE_SUB('2025-01-08', INTERVAL 7 DAY)"
        ),
        "DATE_FORMAT": (
            "SELECT id FROM shop.orders "
            "WHERE dt = DATE_FORMAT('2025-01-01', '%Y-%m-%d')"
        ),
    }
    checked = {}
    for name, sql in checks.items():
        hive = ctx.mcp_call("validate_sql", {"sql": sql, "dialect": "hive"})
        doris = ctx.mcp_call("validate_sql", {"sql": sql, "dialect": "doris"})
        ctx.require(hive.get("status") == "pass", f"{name} hive: {hive}")
        ctx.require(doris.get("status") == "pass", f"{name} doris: {doris}")
        ctx.require(
            hive.get("issues") == doris.get("issues") == [],
            f"{name}: hive={hive.get('issues')} doris={doris.get('issues')}",
        )
        checked[name] = doris.get("status")
    return checked


def check_validate_sql_schema_is_self_contained(ctx):
    def forbidden_paths(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key in {"$ref", "$defs"}:
                    yield child_path
                yield from forbidden_paths(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from forbidden_paths(child, f"{path}[{index}]")

    tool = next(tool for tool in ctx.mcp.list_tools() if tool.name == "validate_sql")
    schema = tool.inputSchema
    ctx.require(
        schema["properties"]["dialect"]["default"] == "hive",
        f"dialect default changed: {schema['properties']['dialect']}",
    )
    violations = list(forbidden_paths(schema, "inputSchema"))
    ctx.require(not violations, f"validate_sql schema has external refs: {violations}")
    return {"default": "hive", "violations": len(violations)}
