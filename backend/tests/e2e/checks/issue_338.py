"""Issue 338: historical-ticket drafts with unresolved placeholders must not be stored."""

from __future__ import annotations

import uuid
from pathlib import Path

from semantica_dataagent.historical_tickets import find_unresolved_placeholders


def _placeholder_patch(sql: str) -> str:
    return f"# 占位符草稿\n\n## 参考 SQL\n\n```sql\n{sql}\n```\n"


def check_placeholder_ticket_gate_over_rest(ctx):
    """Draft insertion and approval both reject PLACEHOLDER deformations over REST."""
    suffix = uuid.uuid4().hex[:10]
    project_name = f"e2e-338-{suffix}"
    created_project = ctx.rest.post(
        "/api/v1/projects",
        json={"name": project_name, "type": "warehouse"},
    )
    ctx.require(
        created_project.status_code == 201,
        f"could not create isolated project: {created_project.status_code} "
        f"{created_project.text[:300]}",
    )
    project_id = created_project.json()["id"]
    params = {"project": project_id}
    disabled = ctx.rest.put(
        f"/api/v1/projects/{project_id}/auto-build", json={"enabled": False}
    )
    ctx.require(disabled.status_code == 200, disabled.text)

    variants = {
        "exact": "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__PLACEHOLDER__'",
        "lowercase": "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__placeholder__'",
        "bare": "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = 'PLACEHOLDER'",
        "prefix_suffix": (
            "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '__PLACEHOLDER_DATE__'"
        ),
        "inside_string": (
            "SELECT COUNT(1) FROM dm.dm_orders WHERE channel = '__PLACEHOLDER__' "
            "AND dt = '2025-01-01'"
        ),
        "inside_comment": (
            "-- 待替换 __PLACEHOLDER__\n"
            "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '2025-01-01'"
        ),
    }
    slug = f"e2e-placeholder-{suffix}"
    target = f"document:knowledge/historical-tickets/{slug}.md"
    blocked_slugs: set[str] = set()
    try:
        for name, sql in variants.items():
            draft_slug = f"{slug}-{name}"
            blocked_slugs.add(draft_slug)
            draft_target = f"document:knowledge/historical-tickets/{draft_slug}.md"
            created = ctx.rest.post(
                "/api/v1/corrections",
                params=params,
                json={
                    "target": draft_target,
                    "target_kind": "document",
                    "patch": _placeholder_patch(sql),
                    "reason": f"e2e 占位符变形 {name}",
                },
            )
            ctx.require(
                created.status_code == 201,
                f"{name}: could not create pending draft: "
                f"{created.status_code} {created.text[:400]}",
            )
            draft_id = created.json()["id"]
            approval = ctx.rest.post(
                f"/api/v1/corrections/{draft_id}/approve", params=params
            )
            ctx.require(
                approval.status_code == 422,
                f"{name}: placeholder draft was approved: "
                f"{approval.status_code} {approval.text[:400]}",
            )
            ctx.require(
                "占位符" in approval.text,
                f"{name}: wrong rejection: {approval.text[:400]}",
            )
            rejected = ctx.rest.post(
                f"/api/v1/corrections/{draft_id}/reject", params=params
            )
            ctx.require(rejected.status_code == 200, rejected.text[:400])

        legal_texts = {
            "prose_english": (
                "Note: replace the placeholder value with a real business date "
                "before running."
            ),
            "prose_chinese": (
                "说明：SQL 中的 placeholder 仅作示例说明，实际查询请填写具体日期。"
            ),
            "column_name": (
                "SELECT order_id, placeholder_flag FROM dm.dm_orders "
                "WHERE dt = '2026-11-26'"
            ),
            "sql_line_comment": (
                "-- placeholder: filter out test orders\n"
                "SELECT COUNT(1) FROM dm.dm_orders"
            ),
            "sql_block_comment": (
                "/* placeholder comment for future filter */\n"
                "SELECT COUNT(1) FROM dm.dm_orders"
            ),
            "string_literal": "SELECT * FROM dm.dm_orders WHERE tag = 'placeholder'",
            "string_literal_phrase": (
                "SELECT * FROM dm.dm_orders WHERE note = 'default placeholder text'"
            ),
            "realistic_ticket": (
                "# 过滤占位订单统计真实成交额\n\n"
                "- **来源**: 对话点赞沉淀\n"
                "- **涉及表**: dm.dm_orders\n"
                "- **方言**: doris\n\n"
                "## 业务口径与上下文\n\n"
                "排除 `placeholder_flag = 1` 的测试占位记录。\n\n"
                "## 参考 SQL\n\n"
                "```sql\n"
                "SELECT SUM(amount) FROM dm.dm_orders WHERE dt = '2026-11-26' "
                "AND placeholder_flag = 0\n"
                "```\n"
            ),
        }
        legal_approved = 0
        for name, text in legal_texts.items():
            legal_slug = f"{slug}-legal-{name}"
            legal_target = f"document:knowledge/historical-tickets/{legal_slug}.md"
            legal_patch = f"# 合法占位文本 {name}\n\n{text}\n"
            legal_created = ctx.rest.post(
                "/api/v1/corrections",
                params=params,
                json={
                    "target": legal_target,
                    "target_kind": "document",
                    "patch": legal_patch,
                    "reason": f"e2e 合法 placeholder 文本 {name}",
                },
            )
            ctx.require(
                legal_created.status_code == 201,
                f"{name}: legal placeholder text was rejected at draft creation: "
                f"{legal_created.status_code} {legal_created.text[:400]}",
            )
            legal_id = legal_created.json()["id"]
            legal_approval = ctx.rest.post(
                f"/api/v1/corrections/{legal_id}/approve", params=params
            )
            ctx.require(
                legal_approval.status_code == 200,
                f"{name}: legal placeholder text was rejected at approval: "
                f"{legal_approval.status_code} {legal_approval.text[:400]}",
            )
            legal_approved += 1

        valid_patch = (
            "# e2e 正常历史工单\n\n"
            "## 参考 SQL\n\n"
            "```sql\n"
            "SELECT COUNT(1) FROM dm.dm_orders WHERE dt = '2025-01-01'\n"
            "```\n"
        )
        proposal = ctx.rest.post(
            "/api/v1/corrections",
            params=params,
            json={
                "target": target,
                "target_kind": "document",
                "patch": valid_patch,
                "reason": "e2e 合法历史工单对照",
            },
        )
        ctx.require(proposal.status_code == 201, proposal.text[:500])
        proposal_id = proposal.json()["id"]

        approval = ctx.rest.post(
            f"/api/v1/corrections/{proposal_id}/approve",
            params=params,
            json={"patch": valid_patch.replace("2025-01-01", "__PLACEHOLDER__")},
        )
        ctx.require(
            approval.status_code == 422,
            "approval accepted a patch with __PLACEHOLDER__: "
            f"{approval.status_code} {approval.text[:400]}",
        )
        ctx.require("占位符" in approval.text, approval.text[:400])

        listing = ctx.rest.get(
            "/api/v1/historical-tickets", params={**params, "search": slug}
        )
        ctx.require(listing.status_code == 200, listing.text[:400])
        leaked = [
            item["slug"]
            for item in listing.json()["items"]
            if item["slug"] in blocked_slugs
        ]
        ctx.require(
            not leaked,
            f"placeholder ticket reached the historical-ticket library: {leaked}",
        )

        rejected = ctx.rest.post(
            f"/api/v1/corrections/{proposal_id}/reject", params=params
        )
        ctx.require(rejected.status_code == 200, rejected.text[:400])
        return {
            "project_id": project_id,
            "variants_blocked_at_approval": len(variants),
            "legal_placeholder_approved": legal_approved,
            "approval_status": approval.status_code,
        }
    finally:
        listing = ctx.rest.get(
            "/api/v1/historical-tickets", params={**params, "search": slug}
        )
        if listing.status_code == 200:
            for item in listing.json()["items"]:
                ctx.rest.delete(
                    f"/api/v1/historical-tickets/{item['slug']}", params=params
                )


def check_sample_knowledge_has_no_placeholder_false_positive(ctx):
    """The real sample corpus must not be mistaken for unresolved placeholders."""
    root = Path(ctx.source_dir)
    files = sorted(root.rglob("*.md"))
    ctx.require(files, f"no Markdown samples under {root}")
    flagged = {
        str(path.relative_to(root)): find_unresolved_placeholders(
            path.read_text(encoding="utf-8")
        )
        for path in files
    }
    flagged = {path: markers for path, markers in flagged.items() if markers}
    ctx.require(not flagged, f"sample false positives: {flagged}")
    return {"checked_files": len(files)}
