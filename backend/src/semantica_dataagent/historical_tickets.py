"""Searchable historical-ticket knowledge curation, LLM context synthesis, and CRUD API."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import (
    auto_build,
    eval_questions,
    knowledge_synthesizer,
    project_context,
    source_directory,
)
from .api_time import UtcDatetime
from .auth import Requester
from .auth import current_requester as rest_requester
from .db import (
    Clarification,
    CorrectionProposal,
    Token,
    Turn,
    sessions,
)
from .mcp_context import bind_mcp_context
from .project_api import (
    admin_project_scope,
    project_scope,
)
from .semantic_overrides import validate_markdown_relpath

logger = logging.getLogger(__name__)
router = APIRouter()

HISTORICAL_TICKETS_CATEGORY = "historical-tickets"
TicketDialect = Literal["hive", "spark", "doris"]

_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_SQL_BLOCK_RE = re.compile(r"```sql\s*\n([\s\S]*?)```", re.IGNORECASE)
_META_LINE_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*[：:]\s*(.+?)\s*$", re.MULTILINE)
_SLUG_SAFE_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")
_MULTI_DASH_RE = re.compile(r"-{2,}")
_WRAPPED_PLACEHOLDER_RE = re.compile(
    r"_{1,2}\s*[A-Za-z0-9_]*placeholder[A-Za-z0-9_]*\s*_{1,2}", re.IGNORECASE
)
_TOKEN_PLACEHOLDER_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9_]*placeholder(?:_[A-Za-z0-9_]*)?"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

NonEmptyText = Annotated[str, Field(min_length=1, max_length=20000, pattern=r"\S")]


class EvalLeakageError(ValueError):
    """Raised when a candidate historical ticket duplicates an eval benchmark question."""


def find_unresolved_placeholders(text: str) -> list[str]:
    """Return distinct placeholder markers that must be resolved before a ticket is stored.

    Covers the discovery marker and its common deformations: case changes,
    extra prefixes/suffixes inside the double underscores, whitespace around
    the word, and the bare word with the underscores removed. Quoting and
    SQL comments need no special handling because the scan is textual.
    """
    if not text:
        return []
    found: list[str] = []
    for pattern in (_WRAPPED_PLACEHOLDER_RE, _TOKEN_PLACEHOLDER_RE):
        for match in pattern.finditer(text):
            token = match.group(0)
            if token not in found:
                found.append(token)
    return found


def unresolved_placeholder_message(markers: list[str]) -> str:
    """Build the user-facing rejection message for unresolved placeholders."""
    shown = "、".join(markers[:5])
    suffix = f" 等 {len(markers)} 处" if len(markers) > 5 else ""
    return f"历史工单仍含未替换的占位符：{shown}{suffix}；请替换为真实值后再入库或审批"


class HistoricalTicketView(BaseModel):
    slug: str
    path: str
    title: str
    summary: str
    sql_preview: str = ""
    dialect: str = "doris"
    tables: list[str] = Field(default_factory=list)
    source_label: str = "人工录入"
    content: str
    size_bytes: int
    updated_at: UtcDatetime


class HistoricalTicketListView(BaseModel):
    items: list[HistoricalTicketView] = Field(default_factory=list)
    pending_proposal_count: int = 0
    total_tables: int = 0


class HistoricalTicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyText
    caliber: str = Field(default="", max_length=10000)
    reference_sql: str = Field(default="", max_length=20000)
    dialect: TicketDialect = "doris"
    caveats: str = Field(default="", max_length=5000)
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    content: str | None = Field(default=None, max_length=40000)

    @field_validator(
        "title", "caliber", "reference_sql", "caveats", "slug", "content", mode="before"
    )
    @classmethod
    def _strip_strings(cls, value):
        return value.strip() if isinstance(value, str) else value


class HistoricalTicketPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyText | None = None
    caliber: str | None = Field(default=None, max_length=10000)
    reference_sql: str | None = Field(default=None, max_length=20000)
    dialect: TicketDialect | None = None
    caveats: str | None = Field(default=None, max_length=5000)
    content: NonEmptyText | None = None

    @field_validator(
        "title", "caliber", "reference_sql", "caveats", "content", mode="before"
    )
    @classmethod
    def _strip_strings(cls, value):
        return value.strip() if isinstance(value, str) else value


def is_historical_ticket_target(target: str) -> bool:
    """Return True when a correction target points inside the historical-tickets folder."""
    if not isinstance(target, str) or not target.startswith("document:"):
        return False
    rel = target[len("document:") :].strip().strip("/")
    parts = PurePosixPath(rel).parts
    return HISTORICAL_TICKETS_CATEGORY in parts[:-1]


def historical_ticket_placeholder_errors(target: str, patch: str) -> list[str]:
    """Return gate errors when a historical-ticket target still carries placeholders."""
    if not is_historical_ticket_target(target):
        return []
    markers = find_unresolved_placeholders(patch)
    return [unresolved_placeholder_message(markers)] if markers else []


def _tickets_rel_dir(project_id: str) -> tuple[str, str]:
    md_root = knowledge_synthesizer.markdown_root_for_project(project_id)
    rel_dir = f"{md_root}/{HISTORICAL_TICKETS_CATEGORY}"
    return md_root, rel_dir


def _slugify_ticket_name(title: str, fallback: str) -> str:
    cleaned = _SLUG_SAFE_RE.sub("-", (title or "").strip().lower()).strip("-_")
    cleaned = _MULTI_DASH_RE.sub("-", cleaned)
    if len(cleaned) > 48:
        cleaned = cleaned[:48].rstrip("-_")
    if not cleaned or cleaned.startswith("."):
        return fallback
    digest = hashlib.sha256((title or fallback).encode("utf-8")).hexdigest()[:6]
    if f"{cleaned}-{digest}" == "index":
        return fallback
    return f"{cleaned}-{digest}"


def _safe_ticket_slug(raw_slug: str) -> str:
    val = str(raw_slug or "").strip().removesuffix(".md")
    if (
        not val
        or "/" in val
        or "\\" in val
        or ".." in val
        or val.startswith(".")
        or val.lower() == "index"
    ):
        raise ValueError("Invalid historical ticket slug")
    return val


def _extract_section(content: str, heading: str) -> str:
    marker = f"## {heading}"
    idx = content.find(marker)
    if idx < 0:
        return ""
    after = content[idx + len(marker) :]
    next_sec = re.search(r"\n##\s+", after)
    section = after[: next_sec.start()] if next_sec else after
    return section.strip()


def _parse_ticket_markdown(
    rel_path: str, slug: str, content: str, stat_mtime: float
) -> HistoricalTicketView:
    heading_match = _HEADING_RE.search(content)
    title = heading_match.group(1).strip() if heading_match else slug

    sql_match = _SQL_BLOCK_RE.search(content)
    sql_preview = sql_match.group(1).strip() if sql_match else ""

    meta = {
        m.group(1).strip(): m.group(2).strip() for m in _META_LINE_RE.finditer(content)
    }
    dialect = meta.get("方言", "doris").strip().lower()
    if dialect not in ("hive", "spark", "doris"):
        dialect = "doris"
    source_label = meta.get("来源", "人工录入").strip() or "人工录入"

    tables: list[str] = []
    if sql_preview:
        try:
            tables, _ = eval_questions.extract_tables_and_fields(sql_preview, dialect)
        except ValueError:
            tables = []
    if not tables and meta.get("涉及表"):
        tables = [
            item.strip().strip("`")
            for item in re.split(r"[,，、\s]+", meta["涉及表"])
            if item.strip().strip("`") and item.strip() != "无"
        ]

    summary_src = _extract_section(content, "业务口径与上下文")
    if not summary_src:
        body_lines = [
            ln.strip()
            for ln in content.splitlines()
            if ln.strip() and not ln.strip().startswith(("#", "-", "*", "```"))
        ]
        summary_src = " ".join(body_lines)
    summary = summary_src[:240]

    return HistoricalTicketView(
        slug=slug,
        path=rel_path,
        title=title,
        summary=summary,
        sql_preview=sql_preview,
        dialect=dialect,
        tables=tables,
        source_label=source_label,
        content=content,
        size_bytes=len(content.encode("utf-8")),
        updated_at=datetime.fromtimestamp(stat_mtime, tz=UTC),
    )


def render_ticket_markdown(
    *,
    title: str,
    caliber: str,
    reference_sql: str,
    dialect: str = "doris",
    caveats: str = "",
    source_label: str = "人工录入",
    context_turns: list[dict[str, Any]] | None = None,
) -> str:
    """Render a clean, self-contained Markdown document for a historical ticket."""
    tables: list[str] = []
    if reference_sql.strip():
        try:
            tables, _ = eval_questions.extract_tables_and_fields(
                reference_sql.strip(), dialect
            )
        except ValueError:
            tables = []
    tables_text = ", ".join(tables) if tables else "见参考 SQL"
    lines = [
        f"# {title.strip()}",
        "",
        f"- **来源**: {source_label}",
        f"- **涉及表**: {tables_text}",
        f"- **方言**: {dialect}",
        "",
        "## 业务口径与上下文",
        "",
        (caliber.strip() or title.strip()),
    ]
    prior_turns = [t for t in (context_turns or []) if not t.get("is_target")]
    if prior_turns:
        lines.extend(["", "### 对话澄清与前置背景", ""])
        for item in prior_turns:
            q_txt = str(item.get("question") or "").strip()
            a_txt = str(item.get("answer") or "").strip()
            if q_txt:
                lines.append(f"- **第 {item.get('sequence', '?')} 轮追问**：{q_txt}")
            if a_txt:
                lines.append(f"  - 回复摘要：{a_txt[:300]}")
            for clar in item.get("clarifications") or []:
                cq = str(clar.get("question") or "").strip()
                ca = str(clar.get("answer") or "").strip()
                if cq and ca:
                    lines.append(f"  - 澄清确认（{cq}）：{ca}")
    if reference_sql.strip():
        lines.extend(
            [
                "",
                "## 参考 SQL",
                "",
                "```sql",
                reference_sql.strip(),
                "```",
            ]
        )
    if caveats.strip():
        lines.extend(["", "## 注意事项与避坑点", "", caveats.strip()])
    lines.append("")
    return "\n".join(lines)


def list_historical_tickets_on_disk(project_id: str) -> list[HistoricalTicketView]:
    """List all committed historical-ticket Markdown documents in the project source tree."""
    src_dir = project_context.source_directory_value(project_id)
    if not src_dir:
        return []
    _, rel_dir = _tickets_rel_dir(project_id)
    with source_directory.preview_repository(src_dir) as root:
        ticket_dir = source_directory.safe(root, source_directory.relative(rel_dir))
        if not ticket_dir.is_dir():
            return []
        items: list[HistoricalTicketView] = []
        for entry in sorted(ticket_dir.iterdir()):
            if (
                not entry.is_file()
                or entry.is_symlink()
                or entry.suffix.lower() != ".md"
                or entry.name.startswith(".")
                or entry.name.lower() == "index.md"
            ):
                continue
            try:
                content = entry.read_text(encoding="utf-8")
                stat = entry.stat()
            except OSError:
                continue
            rel_path = f"{rel_dir}/{entry.name}"
            items.append(
                _parse_ticket_markdown(rel_path, entry.stem, content, stat.st_mtime)
            )
    items.sort(key=lambda item: item.updated_at, reverse=True)
    return items


def check_eval_benchmark_leakage(
    project_id: str, *, question: str, sql: str, dialect: str
) -> dict[str, Any] | None:
    """Return matching eval benchmark question if a candidate duplicates eval/questions.yaml."""
    return eval_questions.find_duplicate_eval_question(
        project_id,
        question=question,
        reference_sql=sql,
        dialect=dialect,
    )


def collect_conversation_context(
    db, conversation_id: str, turn_id: int
) -> dict[str, Any]:
    """Collect sanitized multi-turn Q&A and clarifications up to the target turn."""
    all_turns = (
        db.query(Turn)
        .filter(Turn.conversation_id == conversation_id)
        .order_by(Turn.sequence.asc(), Turn.id.asc())
        .all()
    )
    target = next((item for item in all_turns if item.id == turn_id), None)
    if target is None:
        return {"turn_count": 0, "turns": [], "clarifications": []}
    turns_up_to = [
        item
        for item in all_turns
        if (item.sequence, item.id) <= (target.sequence, target.id)
    ][-6:]
    clars = (
        db.query(Clarification)
        .filter(Clarification.conversation_id == conversation_id)
        .order_by(Clarification.id.asc())
        .all()
    )
    clar_list: list[dict[str, Any]] = []
    for c in clars:
        try:
            parsed = json.loads(c.questions or "[]")
        except (TypeError, ValueError):
            continue
        entries = parsed if isinstance(parsed, list) else [parsed]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            q_text = str(entry.get("question") or "").strip()[:400]
            a_text = str(entry.get("answer") or entry.get("selected") or "").strip()[
                :400
            ]
            opts = [
                str(opt.get("label") or "").strip()[:120]
                for opt in (entry.get("options") or [])
                if isinstance(opt, dict) and str(opt.get("label") or "").strip()
            ]
            if not (q_text or a_text or opts):
                continue
            item: dict[str, Any] = {
                "question": q_text,
                "answer": a_text,
                "status": str(c.status or ""),
            }
            if opts and not a_text:
                item["options"] = opts
            clar_list.append(item)
    turn_summaries: list[dict[str, Any]] = []
    for t in turns_up_to:
        sql_texts: list[str] = []
        try:
            for block in json.loads(t.sql or "[]"):
                if isinstance(block, dict) and block.get("sql"):
                    sql_texts.append(str(block["sql"]).strip()[:2000])
        except (TypeError, ValueError):
            pass
        turn_summaries.append(
            {
                "turn_id": t.id,
                "sequence": t.sequence,
                "question": str(t.question or "").strip()[:1000],
                "answer": str(t.answer or "").strip()[:1500],
                "sql_blocks": sql_texts,
                "is_target": t.id == turn_id,
                "clarifications": clar_list if t.id == turn_id else [],
            }
        )
    return {
        "turn_count": len(turn_summaries),
        "turns": turn_summaries,
        "clarifications": clar_list,
    }


def _build_ticket_llm_prompt(md_root: str) -> str:
    ticket_root = f"{md_root}/{HISTORICAL_TICKETS_CATEGORY}"
    return (
        "你是企业级数仓知识运营专家，负责把多轮问数对话或高频有效查询提炼为可检索、自洽的「历史工单」Markdown 文档。\n"
        "请结合完整多轮对话上下文（包含前置追问、澄清口径、用户补充限制条件）、通过校验的 SQL 与现有工单目录，输出高质量工单。\n"
        "要求：\n"
        "1. 标题与业务口径必须**自洽完整**——把多轮追问/澄清里的隐藏条件（时间范围、业务状态过滤、主体口径、排除项等）全部合并写清，不能依赖“上一轮”之类指代。\n"
        "2. 严谨保留 SQL 逻辑、表名、字段名与常量值，不得臆造不存在的表或字段。\n"
        f'3. 目标路径必须在 `{ticket_root}/<slug>.md` 下：若与已有历史工单高度同主题可合并更新，设 route="existing_document" 并使用对应 target；否则设 route="new_document" 并给出简短语义化 slug。\n'
        "4. 输出严格纯 JSON 对象（禁止输出 Markdown 围栏以外的多余文字），格式如下：\n"
        "{\n"
        '  "route": "new_document|existing_document",\n'
        '  "slug": "简短英文或中文标识-不含.md",\n'
        f'  "target": "document:{ticket_root}/<slug>.md",\n'
        '  "title": "提炼后的标准业务问题标题",\n'
        '  "reason": "一句话说明沉淀理由与提炼要点（用作纠错审批说明）",\n'
        '  "patch": "完整规范 Markdown 内容（包含 # 标题、- **来源**/**涉及表**/**方言**、## 业务口径与上下文、## 参考 SQL、## 注意事项与避坑点）"\n'
        "}"
    )


def synthesize_historical_ticket(
    project_id: str,
    *,
    source_kind: Literal["feedback", "discovery"],
    fallback_title: str,
    fallback_caliber: str,
    reference_sql: str,
    dialect: str,
    context_turns: list[dict[str, Any]] | None = None,
    clarifications: list[dict[str, Any]] | None = None,
    discovery_evidence: dict[str, Any] | None = None,
    user_notes: str = "",
) -> dict[str, Any]:
    """Synthesize a high-quality Markdown ticket via configured LLM, with structured fallback."""
    md_root, rel_dir = _tickets_rel_dir(project_id)
    source_label = "对话点赞沉淀" if source_kind == "feedback" else "冷启动高频查询发现"
    fallback_slug = _slugify_ticket_name(
        fallback_title,
        fallback=f"{source_kind}-{hashlib.sha256(reference_sql.encode('utf-8')).hexdigest()[:8]}",
    )

    existing_tickets = list_historical_tickets_on_disk(project_id)
    existing_brief = [
        {
            "target": f"document:{item.path}",
            "title": item.title,
            "tables": item.tables[:8],
            "summary": item.summary[:160],
        }
        for item in existing_tickets[:25]
    ]

    provider_cfg = knowledge_synthesizer.resolve_llm_provider(
        project_id,
        source_type="discovery" if source_kind == "discovery" else "manual",
    )
    llm_payload = {
        "source_kind": source_kind,
        "source_label": source_label,
        "target_directory": rel_dir,
        "target_question": fallback_title,
        "user_caliber_note": fallback_caliber,
        "user_notes": user_notes,
        "validated_sql": reference_sql,
        "dialect": dialect,
        "conversation_turns": context_turns or [],
        "clarifications": clarifications or [],
        "discovery_evidence": discovery_evidence or {},
        "existing_historical_tickets": existing_brief,
    }
    raw_llm = knowledge_synthesizer.invoke_llm_json(
        provider_cfg,
        _build_ticket_llm_prompt(md_root),
        llm_payload,
    )

    if isinstance(raw_llm, dict) and str(raw_llm.get("patch") or "").strip():
        candidate_target = str(raw_llm.get("target") or "").strip()
        candidate_slug = str(raw_llm.get("slug") or "").strip()
        resolved_target: str | None = None
        if candidate_target.startswith("document:") and is_historical_ticket_target(
            candidate_target
        ):
            try:
                norm_rel = validate_markdown_relpath(
                    candidate_target[len("document:") :],
                    markdown_root=md_root,
                    require_category_subdir=True,
                )
                resolved_target = f"document:{norm_rel}"
            except ValueError:
                resolved_target = None
        if resolved_target is None and candidate_slug:
            safe_slug = _slugify_ticket_name(candidate_slug, fallback_slug)
            resolved_target = f"document:{rel_dir}/{safe_slug}.md"
        if resolved_target is None:
            resolved_target = f"document:{rel_dir}/{fallback_slug}.md"

        title = str(raw_llm.get("title") or fallback_title).strip() or fallback_title
        reason = (
            str(raw_llm.get("reason") or "").strip()
            or f"由{source_label}经上下文大模型提炼为历史工单：{title[:80]}"
        )
        return {
            "target": resolved_target,
            "title": title,
            "reason": reason[:400],
            "patch": str(raw_llm["patch"]).strip() + "\n",
            "synthesis_mode": "llm",
        }

    # Graceful fallback when LLM provider is absent or returns invalid JSON
    fallback_md = render_ticket_markdown(
        title=fallback_title,
        caliber=fallback_caliber,
        reference_sql=reference_sql,
        dialect=dialect,
        caveats=user_notes,
        source_label=source_label,
        context_turns=context_turns,
    )
    return {
        "target": f"document:{rel_dir}/{fallback_slug}.md",
        "title": fallback_title,
        "reason": f"由{source_label}整理为历史工单（模板兜底）：{fallback_title[:80]}",
        "patch": fallback_md,
        "synthesis_mode": "fallback",
    }


def _parse_gate_trigger_evidence(gate_report_raw: str | None) -> dict[str, Any]:
    if not gate_report_raw:
        return {}
    try:
        gate = json.loads(gate_report_raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(gate, dict):
        return {}
    ev = gate.get("evidence")
    if isinstance(ev, dict) and isinstance(ev.get("trigger_evidence"), dict):
        return dict(ev["trigger_evidence"])
    return {}


def find_feedback_proposal_by_turn(
    project_id: str, conversation_id: str, turn_id: int
) -> CorrectionProposal | None:
    """Return active (pending/approved/auto_applied) feedback proposal for a turn."""
    with sessions()() as db:
        rows = (
            db.query(CorrectionProposal)
            .filter(
                CorrectionProposal.project_id == project_id,
                CorrectionProposal.conversation_id == conversation_id,
                CorrectionProposal.source == "feedback",
                CorrectionProposal.status.in_(("pending", "approved", "auto_applied")),
            )
            .order_by(CorrectionProposal.created_at.desc())
            .all()
        )
        for row in rows:
            trig = _parse_gate_trigger_evidence(row.gate_report)
            if trig.get("turn_id") == turn_id:
                return row
    return None


def list_feedback_proposals_by_turn(
    project_id: str, conversation_id: str
) -> dict[int, dict[str, Any]]:
    """Map turn_id -> active feedback proposal status for a conversation."""
    with sessions()() as db:
        rows = (
            db.query(CorrectionProposal)
            .filter(
                CorrectionProposal.project_id == project_id,
                CorrectionProposal.conversation_id == conversation_id,
                CorrectionProposal.source == "feedback",
                CorrectionProposal.status.in_(("pending", "approved", "auto_applied")),
            )
            .order_by(CorrectionProposal.created_at.asc())
            .all()
        )
        mapping: dict[int, dict[str, Any]] = {}
        for row in rows:
            trig = _parse_gate_trigger_evidence(row.gate_report)
            tid = trig.get("turn_id")
            if isinstance(tid, int):
                mapping[tid] = {
                    "proposal_id": row.id,
                    "status": row.status,
                    "target": row.target,
                }
        return mapping


def propose_ticket_from_feedback(
    project_id: str,
    *,
    conversation_id: str,
    turn_id: int,
    turn_question: str,
    chosen_sql: str,
    chosen_dialect: str,
    user_caliber: str,
    user_notes: str,
    requester: Requester,
) -> dict[str, Any]:
    """Build multi-turn LLM-synthesized historical ticket proposal from a conversation turn."""
    existing = find_feedback_proposal_by_turn(project_id, conversation_id, turn_id)
    if existing is not None:
        trig = _parse_gate_trigger_evidence(existing.gate_report)
        return {
            "proposal_id": existing.id,
            "proposal_status": existing.status,
            "target": existing.target,
            "synthesis_mode": (
                "llm" if trig.get("synthesis_mode") == "llm" else "fallback"
            ),
        }

    leak = check_eval_benchmark_leakage(
        project_id, question=turn_question, sql=chosen_sql, dialect=chosen_dialect
    )
    if leak is not None:
        raise EvalLeakageError(
            "该问答与评测题集（eval/questions.yaml）高度重合，为防止评测基准泄漏，禁止沉淀进历史工单库"
        )

    with sessions()() as db:
        conv_ctx = collect_conversation_context(db, conversation_id, turn_id)

    synthesized = synthesize_historical_ticket(
        project_id,
        source_kind="feedback",
        fallback_title=turn_question.strip(),
        fallback_caliber=user_caliber.strip()
        or f"对话确认口径：{turn_question.strip()}",
        reference_sql=chosen_sql,
        dialect=chosen_dialect,
        context_turns=conv_ctx["turns"],
        clarifications=conv_ctx["clarifications"],
        user_notes=user_notes,
    )

    from . import corrections
    from .semantic_overrides import GateValidationError

    evidence_payload = {
        "question": turn_question.strip(),
        "description": synthesized["title"],
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "turn_count": conv_ctx["turn_count"],
        "reference_sql": chosen_sql,
        "dialect": chosen_dialect,
        "synthesis_mode": synthesized["synthesis_mode"],
        "destination_kind": "historical_ticket",
        "context_excerpts": [
            {
                "sequence": t["sequence"],
                "question": t["question"][:200],
                "is_target": t["is_target"],
            }
            for t in conv_ctx["turns"]
        ],
    }

    with (
        bind_mcp_context(requester),
        corrections.trusted_feedback_source(),
    ):
        try:
            created = corrections.propose_in_project(
                project_id,
                target=synthesized["target"],
                patch=synthesized["patch"],
                reason=synthesized["reason"],
                source="feedback",
                target_kind="document",
                conversation_id=conversation_id,
                evidence=evidence_payload,
            )
        except GateValidationError as exc:
            raise ValueError(str(exc)) from exc
    return {
        "proposal_id": str(created["proposal_id"]),
        "proposal_status": str(created.get("status") or "pending"),
        "target": str(created.get("target") or synthesized["target"]),
        "synthesis_mode": synthesized["synthesis_mode"],
    }


def propose_ticket_from_discovery(
    project_id: str,
    *,
    run_id: str,
    tpl: Any,
    candidate_sql: str,
    eval_dialect: str,
    fallback_title: str,
    fallback_caliber: str,
    requester: Requester,
) -> tuple[str, str | None, str | None, dict[str, Any]]:
    """Create a pending historical-ticket proposal from a discovery query template."""
    leak = check_eval_benchmark_leakage(
        project_id, question=fallback_title, sql=candidate_sql, dialect=eval_dialect
    )
    if leak is not None:
        return (
            "deduplicated",
            None,
            "eval_benchmark_leakage_guard",
            {"matched_eval_id": leak.get("id")},
        )

    fingerprint = str(getattr(tpl, "fingerprint", "") or "")
    with sessions()() as db:
        pending_rows = (
            db.query(CorrectionProposal)
            .filter(
                CorrectionProposal.project_id == project_id,
                CorrectionProposal.source == "discovery",
                CorrectionProposal.status.in_(("pending", "approved", "auto_applied")),
            )
            .all()
        )
        for row in pending_rows:
            if not is_historical_ticket_target(row.target):
                continue
            trig = _parse_gate_trigger_evidence(row.gate_report)
            if fingerprint and trig.get("fingerprint") == fingerprint:
                return ("deduplicated", row.id, "duplicate_historical_ticket", {})

    disc_ev = {
        "run_id": run_id,
        "candidate_kind": "historical_ticket",
        "fingerprint": fingerprint,
        "tables": list(getattr(tpl, "tables", ()) or ()),
        "dialect": eval_dialect,
        "query_history_frequency": int(getattr(tpl, "execution_count", 0) or 0),
        "distinct_callers": int(getattr(tpl, "distinct_callers", 0) or 0),
        "probing_status": "not_applicable",
    }

    synthesized = synthesize_historical_ticket(
        project_id,
        source_kind="discovery",
        fallback_title=fallback_title,
        fallback_caliber=fallback_caliber,
        reference_sql=candidate_sql,
        dialect=eval_dialect,
        discovery_evidence=disc_ev,
        user_notes=f"来自查询历史聚类发现（运行 {run_id}，执行频次 {disc_ev['query_history_frequency']}）。",
    )
    disc_ev["question"] = synthesized["title"]
    disc_ev["reference_sql"] = candidate_sql
    disc_ev["synthesis_mode"] = synthesized["synthesis_mode"]
    disc_ev["destination_kind"] = "historical_ticket"

    from . import corrections
    from .semantic_overrides import GateValidationError

    try:
        with (
            bind_mcp_context(requester),
            corrections.trusted_discovery_source(),
        ):
            created = corrections.propose_in_project(
                project_id,
                target=synthesized["target"],
                patch=synthesized["patch"],
                reason=synthesized["reason"],
                source="discovery",
                target_kind="document",
                extra_gate_report={"discovery_evidence": disc_ev},
                evidence=disc_ev,
            )
    except GateValidationError as exc:
        return ("rejected", None, str(exc), disc_ev)
    except ValueError as exc:
        msg = str(exc)
        if "does not change" in msg:
            return ("deduplicated", None, "duplicate_historical_ticket", disc_ev)
        return ("rejected", None, msg, disc_ev)

    return ("proposed", str(created["proposal_id"]), None, disc_ev)


def _git_author_for_requester(requester: Requester) -> str:
    from . import corrections

    with sessions()() as db:
        tok = db.get(Token, requester.id)
        name = tok.name if tok is not None else requester.id
    return corrections._validate_git_author(name)


def _ensure_source_dir(project_id: str) -> str:
    src_dir = project_context.source_directory_value(project_id)
    if not src_dir:
        raise HTTPException(422, detail="Project source_directory is not configured")
    return src_dir


@router.get("/api/v1/historical-tickets", response_model=HistoricalTicketListView)
def list_historical_tickets(
    scope: Annotated[project_context.ProjectContext, Depends(project_scope)],
    search: Annotated[str | None, Query()] = None,
):
    items = list_historical_tickets_on_disk(scope.project_id)
    table_set: set[str] = set()
    for item in items:
        table_set.update(item.tables)

    if search and search.strip():
        needle = search.strip().lower()
        items = [
            item
            for item in items
            if needle in item.title.lower()
            or needle in item.summary.lower()
            or needle in item.sql_preview.lower()
            or any(needle in tbl.lower() for tbl in item.tables)
            or needle in item.slug.lower()
        ]

    with sessions()() as db:
        pending_rows = (
            db.query(CorrectionProposal)
            .filter(
                CorrectionProposal.project_id == scope.project_id,
                CorrectionProposal.status == "pending",
            )
            .all()
        )
        pending_count = sum(
            1 for row in pending_rows if is_historical_ticket_target(row.target)
        )

    return HistoricalTicketListView(
        items=items,
        pending_proposal_count=pending_count,
        total_tables=len(table_set),
    )


@router.post(
    "/api/v1/historical-tickets",
    response_model=HistoricalTicketView,
    status_code=201,
)
def create_historical_ticket(
    body: HistoricalTicketCreate,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[Requester, Depends(rest_requester)],
):
    src_dir = _ensure_source_dir(scope.project_id)
    try:
        author = _git_author_for_requester(requester)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    if body.reference_sql.strip():
        ok, err = eval_questions.validate_sql_if_available(
            body.reference_sql, body.dialect, scope.project_id
        )
        if not ok:
            raise HTTPException(422, detail=err or "reference_sql validation failed")

    leak = check_eval_benchmark_leakage(
        scope.project_id,
        question=body.title,
        sql=body.reference_sql,
        dialect=body.dialect,
    )
    if leak is not None:
        raise HTTPException(
            409,
            detail="该工单与评测题集（eval/questions.yaml）重合，禁止直接录入可检索工单库",
        )

    try:
        slug = (
            _safe_ticket_slug(body.slug)
            if body.slug
            else _slugify_ticket_name(
                body.title,
                fallback=f"ticket-{hashlib.sha256(body.title.encode('utf-8')).hexdigest()[:8]}",
            )
        )
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    md_root, rel_dir = _tickets_rel_dir(scope.project_id)
    content = (
        body.content.strip() + "\n"
        if body.content and body.content.strip()
        else render_ticket_markdown(
            title=body.title,
            caliber=body.caliber,
            reference_sql=body.reference_sql,
            dialect=body.dialect,
            caveats=body.caveats,
            source_label="人工录入",
        )
    )
    markers = find_unresolved_placeholders(content)
    if markers:
        raise HTTPException(422, detail=unresolved_placeholder_message(markers))
    rel_file = f"{rel_dir}/{slug}.md"
    validate_markdown_relpath(
        rel_file, markdown_root=md_root, require_category_subdir=True
    )

    with source_directory.repository(src_dir) as root:
        dest = source_directory.safe(root, source_directory.relative(rel_file))
        if dest.exists():
            raise HTTPException(409, detail="Historical ticket slug already exists")
        source_directory.upload(
            root,
            rel_dir,
            {f"{slug}.md": content.encode("utf-8")},
            replace=False,
            author=author,
            category_root=md_root,
        )
        stat = dest.stat()

    auto_build.request_upload_build(scope.project_id, author)
    return _parse_ticket_markdown(rel_file, slug, content, stat.st_mtime)


@router.patch(
    "/api/v1/historical-tickets/{slug}",
    response_model=HistoricalTicketView,
)
def update_historical_ticket(
    slug: str,
    body: HistoricalTicketPatch,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[Requester, Depends(rest_requester)],
):
    src_dir = _ensure_source_dir(scope.project_id)
    try:
        safe_slug = _safe_ticket_slug(slug)
        author = _git_author_for_requester(requester)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    md_root, rel_dir = _tickets_rel_dir(scope.project_id)
    rel_file = f"{rel_dir}/{safe_slug}.md"

    with source_directory.repository(src_dir) as root:
        dest = source_directory.safe(root, source_directory.relative(rel_file))
        if not dest.is_file():
            raise HTTPException(404, detail="Historical ticket not found")
        current_content = dest.read_text(encoding="utf-8")
        current_view = _parse_ticket_markdown(
            rel_file, safe_slug, current_content, dest.stat().st_mtime
        )

        if body.content is not None:
            next_content = body.content.strip() + "\n"
            check_title = _HEADING_RE.search(next_content)
            parsed_preview = _parse_ticket_markdown(
                rel_file, safe_slug, next_content, dest.stat().st_mtime
            )
            check_q = (
                check_title.group(1).strip() if check_title else current_view.title
            )
            check_sql = parsed_preview.sql_preview
            check_dialect = parsed_preview.dialect
        else:
            next_title = body.title if body.title is not None else current_view.title
            next_caliber = (
                body.caliber
                if body.caliber is not None
                else (
                    _extract_section(current_content, "业务口径与上下文")
                    or current_view.summary
                )
            )
            next_sql = (
                body.reference_sql
                if body.reference_sql is not None
                else current_view.sql_preview
            )
            next_dialect = (
                body.dialect if body.dialect is not None else current_view.dialect
            )
            next_caveats = (
                body.caveats
                if body.caveats is not None
                else _extract_section(current_content, "注意事项与避坑点")
            )
            next_content = render_ticket_markdown(
                title=next_title,
                caliber=next_caliber,
                reference_sql=next_sql,
                dialect=next_dialect,
                caveats=next_caveats,
                source_label=current_view.source_label,
            )
            check_q = next_title
            check_sql = next_sql
            check_dialect = next_dialect

        markers = find_unresolved_placeholders(next_content)
        if markers:
            raise HTTPException(422, detail=unresolved_placeholder_message(markers))

        if check_sql.strip():
            ok, err = eval_questions.validate_sql_if_available(
                check_sql, check_dialect, scope.project_id
            )
            if not ok:
                raise HTTPException(
                    422, detail=err or "reference_sql validation failed"
                )

        leak = check_eval_benchmark_leakage(
            scope.project_id,
            question=check_q,
            sql=check_sql,
            dialect=check_dialect,
        )
        if leak is not None:
            raise HTTPException(
                409,
                detail="修改内容与评测题集（eval/questions.yaml）重合，禁止写入历史工单库",
            )

        source_directory.upload(
            root,
            rel_dir,
            {f"{safe_slug}.md": next_content.encode("utf-8")},
            replace=False,
            author=author,
            category_root=md_root,
        )
        stat = dest.stat()

    auto_build.request_upload_build(scope.project_id, author)
    return _parse_ticket_markdown(rel_file, safe_slug, next_content, stat.st_mtime)


@router.delete("/api/v1/historical-tickets/{slug}", status_code=204)
def delete_historical_ticket(
    slug: str,
    scope: Annotated[project_context.ProjectContext, Depends(admin_project_scope)],
    requester: Annotated[Requester, Depends(rest_requester)],
):
    src_dir = _ensure_source_dir(scope.project_id)
    try:
        safe_slug = _safe_ticket_slug(slug)
        author = _git_author_for_requester(requester)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    _, rel_dir = _tickets_rel_dir(scope.project_id)
    rel_file = f"{rel_dir}/{safe_slug}.md"

    with source_directory.repository(src_dir) as root:
        dest = source_directory.safe(root, source_directory.relative(rel_file))
        if not dest.is_file():
            raise HTTPException(404, detail="Historical ticket not found")
        dest.unlink()
        try:
            source_directory.git(root, "rev-parse", "--verify", "HEAD")
            has_head = True
        except ValueError:
            has_head = False
        if has_head:
            source_directory.git(root, "add", "-A", "--", rel_file)
            source_directory.git(
                root,
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                f"删除历史工单 {safe_slug}.md",
                author=author,
            )

    auto_build.request_upload_build(scope.project_id, author)
    return Response(status_code=204)
