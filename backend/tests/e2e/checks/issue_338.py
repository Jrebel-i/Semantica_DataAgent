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
    try:
        for name, sql in variants.items():
            draft_slug = f"{slug}-{name}"
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
        ctx.require(
            not listing.json()["items"],
            "placeholder ticket reached the historical-ticket library",
        )

        rejected = ctx.rest.post(
            f"/api/v1/corrections/{proposal_id}/reject", params=params
        )
        ctx.require(rejected.status_code == 200, rejected.text[:400])
        return {
            "project_id": project_id,
            "variants_blocked_at_approval": len(variants),
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
