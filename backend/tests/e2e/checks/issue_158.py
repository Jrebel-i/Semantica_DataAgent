"""Check the admin-page data paths and shipped admin UI rules for issue 158.

This ticket only changes the web workbench, so the REST checks exercise the
endpoints behind the replaced controls. The static checks make sure the built
workbench keeps the admin-only styling markers that a browser smoke test would
otherwise observe.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

ADMIN_PAGES = (
    "BuildPage.tsx",
    "SourcePage.tsx",
    "CorrectionsPage.tsx",
    "PluginsPage.tsx",
    "TokensPage.tsx",
    "McpAccessPage.tsx",
    "AgentSettings.tsx",
    "PlatformSettings.tsx",
    "StatusPage.tsx",
    "KnowledgePage.tsx",
    "SearchPage.tsx",
    "PersonalSettingsPage.tsx",
)


def _get(ctx, path, **params):
    response = ctx.rest.get(path, params=params or None)
    ctx.require(response.status_code == 200, f"{path}: {response.text}")
    return response.json()


def check_admin_page_contract(ctx):
    tables = _get(ctx, "/api/v1/tables", view="summary")
    ctx.require(tables, "no tables returned for the knowledge page")
    first = tables[0]
    detail = _get(
        ctx,
        f"/api/v1/tables/{first['database']}/{first['name']}",
    )
    fields = detail.get("fields") or []
    ctx.require(fields, "table detail has no fields for the knowledge table")
    ctx.require(
        all({"name", "type", "comment", "partition"} <= set(field) for field in fields),
        "table detail field shape changed",
    )

    settings = _get(ctx, "/api/v1/settings")
    ctx.require(isinstance(settings, dict), "settings response is not an object")

    for path in (
        "/api/v1/builds",
        "/api/v1/generations",
        "/api/v1/plugins",
        "/api/v1/tokens",
        "/api/v1/corrections",
        "/api/v1/knowledge-gaps",
    ):
        _get(ctx, path)

    plugins = _get(ctx, "/api/v1/plugins")
    if plugins:
        plugin = plugins[0]
        url = f"/api/v1/plugins/{plugin['category']}/{plugin['id']}"
        original = bool(plugin["enabled"])
        response = ctx.rest.put(
            url,
            json={"enabled": not original, "parameters": plugin["parameters"]},
        )
        ctx.require(response.status_code == 200, response.text)
        ctx.require(
            response.json()["enabled"] is (not original),
            "plugin checkbox save did not persist the enabled flag",
        )
        response = ctx.rest.put(
            url,
            json={"enabled": original, "parameters": plugin["parameters"]},
        )
        ctx.require(response.status_code == 200, response.text)
        ctx.require(
            response.json()["enabled"] is original,
            "plugin checkbox state was not restored",
        )

    for page in ADMIN_PAGES:
        source = (ROOT / "web/src" / page).read_text()
        ctx.require("admin-page" in source, f"{page} is missing the admin-page scope")

    css = (ROOT / "web/src/workbench.css").read_text()
    markers = (
        "/* 管理类页面 */",
        ".admin-page .file-picker .ant-upload",
        ".admin-page .page-title",
        ".admin-page .card-title",
        "padding-bottom: calc(var(--status-bar-height) + var(--admin-space-24))",
        ".admin-page .knowledge-table-scroll",
        ".admin-page .knowledge-table-scroll .data-table td.field-name",
        "min-width: 180px;",
        "min-width: 130px;",
        ".form-card > label:not(.ant-checkbox-wrapper)",
        ".admin-page .empty-state",
        "--admin-space-8: 8px;",
        "--admin-space-12: 12px;",
        "--admin-space-16: 16px;",
        "--admin-space-24: 24px;",
    )
    for marker in markers:
        ctx.require(marker in css, f"workbench.css is missing {marker!r}")
    ctx.require(
        css.index("/* 管理类页面 */") > css.index("/* Conversation */"),
        "admin styles are not in the trailing workbench section",
    )

    source_page = (ROOT / "web/src/SourcePage.tsx").read_text()
    ctx.require("<Upload" in source_page, "source page no longer uses antd Upload")
    for page in ("PluginsPage.tsx", "SourcePage.tsx", "AgentSettings.tsx"):
        ctx.require(
            "<Checkbox" in (ROOT / "web/src" / page).read_text(),
            f"{page} no longer uses antd Checkbox",
        )
    ctx.require(
        "knowledge-table-scroll" in (ROOT / "web/src/KnowledgePage.tsx").read_text(),
        "knowledge page lost its horizontal table container",
    )
    knowledge_page = (ROOT / "web/src/KnowledgePage.tsx").read_text()
    for column in (
        'className="field-name"',
        'className="field-type"',
        'className="field-comment"',
        'className="field-dimension"',
        'className="field-partition"',
    ):
        ctx.require(
            column in knowledge_page,
            f"knowledge field table lost column marker {column}",
        )

    return {
        "tables": len(tables),
        "fields": len(fields),
        "plugins": len(plugins),
    }
