"""TraceSource-based offline knowledge evolution runner and crash recovery (ADR-0013)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from . import (
    corrections,
    knowledge_synthesizer,
    plugins,
    project_context,
    pull_instances,
    trace_sources,
)
from .api_time import as_utc as _as_utc
from .auth import Requester
from .crypto import decrypt
from .db import (
    EvolutionRun,
    ProjectEvolutionConfig,
    Setting,
    sessions,
)
from .mcp_context import bind_mcp_context
from .plugins import TraceConversation, TraceMessage, TraceQuery
from .semantic_overrides import GateValidationError

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 10
_MAX_MESSAGE_CHARS = 1500
_MAX_SUMMARY_EXCERPT_CHARS = 240
_MAX_SUMMARY_MESSAGES = 6
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*\n([\s\S]*?)```", re.IGNORECASE)
_EMPTY_PROPOSALS_MSG = "大模型未返回可用的知识提案，请重试或切换为手工编辑"


class EvolutionConflictError(RuntimeError):
    """Raised when attempting to start a concurrent evolution run on one project."""


class EvolutionFrozenError(RuntimeError):
    """Raised when attempting to run evolution on an archived or deleting project."""


class EvolutionConfigError(ValueError):
    """Raised when trace source configuration or credentials are missing/invalid."""


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _as_utc(dt).isoformat().replace("+00:00", "Z")


def _parse_utc(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _as_utc(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text_val = raw.strip()
    if text_val.endswith("Z"):
        text_val = text_val[:-1] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text_val))
    except ValueError:
        return None


def _scrub_error(exc: Exception | str, secrets: Mapping[str, str] = ()) -> str:
    secret_vals = tuple(str(v) for v in secrets.values() if v)
    cleaned = trace_sources.scrub_trace_text(str(exc), secret_vals)
    return (cleaned or type(exc).__name__)[:500]


def serialize_run(row: EvolutionRun) -> dict[str, Any]:
    """Serialize an EvolutionRun ORM row into the clean ADR-0013 run view."""
    summary_data = _loads_json(row.signal_summary, {})
    if not isinstance(summary_data, dict):
        summary_data = {}
    proposal_ids = summary_data.get("proposal_ids")
    if not isinstance(proposal_ids, list):
        proposal_ids = [row.proposal_id] if row.proposal_id else []
    else:
        proposal_ids = [str(item) for item in proposal_ids if item]

    return {
        "id": row.id,
        "project_id": row.project_id,
        "trigger_type": row.trigger_type,
        "status": row.status,
        "trace_source_plugin": summary_data.get("trace_source_plugin"),
        "traces_fetched": int(summary_data.get("traces_fetched", 0) or 0),
        "proposals_created": int(summary_data.get("proposals_created", 0) or 0),
        "duplicates_skipped": int(summary_data.get("duplicates_skipped", 0) or 0),
        "l1_rejected": int(summary_data.get("l1_rejected", 0) or 0),
        "proposal_ids": proposal_ids,
        "proposal_id": row.proposal_id,
        "window_since": _parse_utc(summary_data.get("window_since")),
        "window_until": _parse_utc(summary_data.get("window_until")),
        "summary": str(summary_data.get("summary") or ""),
        "stop_reason": row.stop_reason,
        "error": row.error,
        "created_by": row.created_by,
        "started_at": _as_utc(row.started_at),
        "finished_at": _as_utc(row.finished_at),
        "created_at": _as_utc(row.created_at),
    }


def recover_interrupted_evolution_runs() -> int:
    """Mark leftover running evolution runs as incomplete on application startup."""
    now = datetime.now(UTC)
    with sessions().begin() as db:
        result = db.execute(
            update(EvolutionRun)
            .where(EvolutionRun.status == "running")
            .values(
                status="incomplete",
                stop_reason="interrupted",
                error="Evolution run interrupted by application restart",
                finished_at=now,
            )
        )
        return int(result.rowcount or 0)


def has_global_langfuse_fallback(db=None) -> bool:
    """Return True if global Langfuse settings are enabled and complete."""
    if db is None:
        with sessions()() as own:
            return has_global_langfuse_fallback(own)
    keys = (
        "langfuse_enabled",
        "langfuse_url",
        "langfuse_public_key",
        "langfuse_secret_key",
    )
    vals = {
        row.key: row.value
        for row in db.query(Setting).filter(Setting.key.in_(keys)).all()
    }
    return (
        vals.get("langfuse_enabled") == "true"
        and bool((vals.get("langfuse_url") or "").strip())
        and bool((vals.get("langfuse_public_key") or "").strip())
        and bool((vals.get("langfuse_secret_key") or "").strip())
    )


def resolve_trace_source(
    project_id: str,
    *,
    discovered_plugins: Mapping[tuple[str, str], Any] | None = None,
) -> tuple[Any, str, dict[str, Any], dict[str, str]]:
    """Resolve the active TraceSourcePlugin, public parameters, and decrypted secrets."""
    registry = (
        discovered_plugins if discovered_plugins is not None else plugins.discover()
    )

    with sessions()() as db:
        cfg = db.get(ProjectEvolutionConfig, project_id)
        plugin_id = (
            (cfg.trace_source_plugin or "").strip() if cfg is not None else ""
        ) or None
        if plugin_id:
            plugin = registry.get(("trace_source", plugin_id))
            if plugin is None:
                raise EvolutionConfigError(
                    f"未安装对话追踪源插件：{plugin_id}，请检查插件安装或更换项目配置"
                )
            raw_params = _loads_json(cfg.trace_source_parameters if cfg else "{}", {})
            if not isinstance(raw_params, dict):
                raw_params = {}
            secret_keys = plugins.secret_parameter_keys(plugin.parameters_schema)
            public_params = {
                k: v for k, v in raw_params.items() if k not in secret_keys
            }
            try:
                secrets = pull_instances.decrypt_instance_secrets(
                    cfg.trace_source_encrypted_secrets if cfg else "{}"
                )
            except (InvalidToken, ValueError) as exc:
                raise EvolutionConfigError(
                    "对话追踪源凭据解密失败，请在项目演化设置中重新保存密钥"
                ) from exc
            return plugin, plugin_id, public_params, secrets

        keys = (
            "langfuse_enabled",
            "langfuse_url",
            "langfuse_public_key",
            "langfuse_secret_key",
        )
        vals = {
            row.key: row.value
            for row in db.query(Setting).filter(Setting.key.in_(keys)).all()
        }
        if (
            vals.get("langfuse_enabled") == "true"
            and (vals.get("langfuse_url") or "").strip()
            and (vals.get("langfuse_public_key") or "").strip()
            and (vals.get("langfuse_secret_key") or "").strip()
        ):
            plugin = registry.get(("trace_source", "langfuse"))
            if plugin is None:
                raise EvolutionConfigError(
                    "全局 Langfuse 已启用，但未安装 langfuse 对话追踪源插件"
                )
            try:
                secret_key = decrypt(vals["langfuse_secret_key"])
            except (InvalidToken, ValueError) as exc:
                raise EvolutionConfigError(
                    "全局 Langfuse Secret Key 解密失败，请重新配置全局或项目级 Langfuse 凭据"
                ) from exc
            return (
                plugin,
                "langfuse",
                {
                    "base_url": vals["langfuse_url"].strip().rstrip("/"),
                    "public_key": vals["langfuse_public_key"].strip(),
                },
                {"secret_key": secret_key},
            )

    raise EvolutionConfigError(
        "未配置对话追踪源：请在项目演化设置中配置 TraceSource 插件（如 Langfuse），或先启用全局 Langfuse 配置"
    )


def _filter_and_scrub_conversations(
    raw_items: Sequence[TraceConversation],
    *,
    project_id: str,
    secrets: Mapping[str, str],
) -> list[TraceConversation]:
    secret_tuple = tuple(str(v) for v in secrets.values() if v)
    clean: list[TraceConversation] = []
    seen_ids: set[str] = set()
    for conv in raw_items:
        if not isinstance(conv, TraceConversation):
            continue
        if conv.project_id != project_id:
            continue
        if not conv.trace_id or conv.trace_id in seen_ids:
            continue
        msgs: list[TraceMessage] = []
        for msg in conv.messages:
            if msg.role not in {"user", "assistant"}:
                continue
            cleaned_text = trace_sources.scrub_trace_text(msg.content, secret_tuple)
            if not cleaned_text:
                continue
            msgs.append(
                TraceMessage(
                    role=msg.role,
                    content=cleaned_text[:_MAX_MESSAGE_CHARS],
                    timestamp=_as_utc(msg.timestamp) if msg.timestamp else None,
                )
            )
        if not msgs:
            continue
        seen_ids.add(conv.trace_id)
        clean.append(
            replace(
                conv,
                messages=msgs,
                started_at=_as_utc(conv.started_at) if conv.started_at else None,
                ended_at=_as_utc(conv.ended_at) if conv.ended_at else None,
            )
        )
    epoch_min = datetime.min.replace(tzinfo=UTC)
    clean.sort(key=lambda c: (c.started_at or epoch_min, c.trace_id))
    return clean


def _fetch_conversations(plugin: Any, query: TraceQuery) -> list[TraceConversation]:
    fetch_fn = getattr(plugin, "fetch_traces", None) or getattr(
        plugin, "fetch_conversations", None
    )
    if not callable(fetch_fn):
        raise EvolutionConfigError(
            f"对话追踪源插件 {getattr(plugin, 'id', 'unknown')} 缺少 fetch_traces 方法"
        )
    result = fetch_fn(query)
    return list(result or ())


def _compute_next_watermark(
    *,
    prev_watermark: datetime | None,
    window_since: datetime | None,
    window_until: datetime,
    conversations: Sequence[TraceConversation],
    limit: int,
) -> datetime | None:
    """Compute safe watermark advancement without skipping gaps or truncated tail traces."""
    if len(conversations) >= limit:
        timestamps = [
            ts
            for ts in (c.ended_at or c.started_at for c in conversations)
            if ts is not None
        ]
        candidate = max(timestamps) if timestamps else window_until
    else:
        candidate = window_until

    candidate_utc = _as_utc(candidate)
    if prev_watermark is None:
        return candidate_utc
    prev_utc = _as_utc(prev_watermark)
    if window_since is not None and _as_utc(window_since) > prev_utc:
        # Manual future-window run leaves [prev_watermark, window_since] unscanned.
        return None
    return candidate_utc if candidate_utc > prev_utc else None


def _build_batch_inputs(
    batch: Sequence[TraceConversation],
    *,
    window_since: datetime | None,
    window_until: datetime,
) -> tuple[str, dict[str, Any], list[dict[str, str]], list[str], list[dict[str, Any]]]:
    trace_ids: list[str] = []
    session_ids: list[str] = []
    seen_sessions: set[str] = set()
    sql_blocks: list[dict[str, str]] = []
    seen_sql: set[str] = set()
    convo_lines: list[str] = []
    message_summary: list[dict[str, Any]] = []

    for conv in batch:
        trace_ids.append(conv.trace_id)
        if conv.session_id and conv.session_id not in seen_sessions:
            seen_sessions.add(conv.session_id)
            session_ids.append(conv.session_id)
        convo_lines.append(
            f"### Trace {conv.trace_id} ({_iso_utc(conv.started_at) or 'unknown'})"
        )
        excerpts: list[dict[str, str]] = []
        for msg in conv.messages:
            convo_lines.append(f"[{msg.role}]: {msg.content}")
            if len(excerpts) < _MAX_SUMMARY_MESSAGES:
                excerpts.append(
                    {
                        "role": msg.role,
                        "content": msg.content[:_MAX_SUMMARY_EXCERPT_CHARS],
                    }
                )
            for sql_match in _SQL_FENCE_RE.findall(msg.content):
                cleaned_sql = sql_match.strip()
                if cleaned_sql and cleaned_sql not in seen_sql and len(sql_blocks) < 10:
                    seen_sql.add(cleaned_sql)
                    sql_blocks.append({"sql": cleaned_sql, "dialect": "doris"})
        message_summary.append(
            {
                "trace_id": conv.trace_id,
                "started_at": _iso_utc(conv.started_at),
                "messages": excerpts,
            }
        )

    description = (
        f"基于 {len(batch)} 条线上人机对话追踪记录（时间窗 {_iso_utc(window_since) or '起始'} ~ "
        f"{_iso_utc(window_until)}），归纳高频业务指标口径、用户隐性纠偏与缺失表/字段说明：\n\n"
        + "\n".join(convo_lines)
    )
    raw_evidence = {
        "trace_ids": trace_ids,
        "session_ids": session_ids,
        "window_since": _iso_utc(window_since),
        "window_until": _iso_utc(window_until),
        "conversation_count": len(batch),
        "message_summary": message_summary,
    }
    return description, raw_evidence, sql_blocks, trace_ids, message_summary


def _persist_evolution_proposal(
    *,
    project_id: str,
    run_id: str,
    plugin_id: str,
    actor: Requester,
    candidate: knowledge_synthesizer.SynthesizedProposal,
    batch_trace_ids: Sequence[str],
    batch_session_ids: Sequence[str],
    window_since: datetime | None,
    window_until: datetime,
    conversation_count: int,
    message_summary: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    trigger_evidence = {
        "evolution_run_id": run_id,
        "trace_source_plugin": plugin_id,
        "trace_ids": list(batch_trace_ids),
        "session_ids": list(batch_session_ids),
        "window_since": _iso_utc(window_since),
        "window_until": _iso_utc(window_until),
        "conversation_count": conversation_count,
        "message_summary": list(message_summary),
        "llm_rationale": candidate.reason,
    }
    extra_gate = {
        "checks": [
            c
            for c in (candidate.gate_report.get("checks") or [])
            if isinstance(c, Mapping) and c.get("name") == "anti_duplication"
        ],
        "provenance": {
            "evolution_run_id": run_id,
            "trace_ids": list(batch_trace_ids),
        },
    }
    eval_candidate = (
        {"reference_sql": candidate.reference_sql} if candidate.reference_sql else None
    )

    def _submit(target_value: str) -> dict[str, Any]:
        with bind_mcp_context(actor), corrections.trusted_evolution_source():
            return corrections.propose_in_project(
                project_id,
                target=target_value,
                patch=candidate.patch,
                reason=candidate.reason,
                source="evolution",
                target_kind=candidate.target_kind,
                structured_patch=candidate.structured_patch,
                eval_candidate=eval_candidate,
                extra_gate_report=extra_gate,
                evidence=trigger_evidence,
            )

    try:
        return _submit(candidate.target)
    except ValueError as exc:
        if (
            candidate.target_kind == "document"
            and candidate.resolved_path
            and "Document target must live under" in str(exc)
        ):
            fallback_target = f"document:{candidate.resolved_path}"
            if fallback_target != candidate.target:
                return _submit(fallback_target)
        raise


def _mark_run_failed(
    run_id: str,
    *,
    stop_reason: str,
    error: str,
    extra_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    finished = datetime.now(UTC)
    with sessions().begin() as db:
        run = db.get(EvolutionRun, run_id)
        if run is None:
            raise LookupError(f"Evolution run {run_id} not found")
        summary_data = _loads_json(run.signal_summary, {})
        if not isinstance(summary_data, dict):
            summary_data = {}
        if extra_summary:
            summary_data.update(extra_summary)
        summary_data.setdefault("summary", error)
        run.status = "failed"
        run.stop_reason = stop_reason
        run.error = error
        run.signal_summary = json.dumps(summary_data, ensure_ascii=False)
        run.finished_at = finished
        db.flush()
        return serialize_run(run)


def create_evolution_run(
    project_id: str,
    *,
    trigger_type: str = "manual",
    created_by: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    max_traces: int | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    claim_scheduled_at: datetime | None = None,
) -> dict[str, Any]:
    """Atomically reserve one running evolution slot for ``project_id``."""
    now = datetime.now(UTC)
    cfg = project_context.evolution_config(project_id)
    eff_max_traces = (
        max(1, min(2000, int(max_traces)))
        if max_traces is not None
        else int(cfg["max_traces_per_run"])
    )
    init_summary = {
        "explicit_since": _iso_utc(since),
        "explicit_until": _iso_utc(until),
        "max_traces": eff_max_traces,
        "provider_id": provider_id.strip()
        if isinstance(provider_id, str) and provider_id.strip()
        else None,
        "model": model.strip() if isinstance(model, str) and model.strip() else None,
        "traces_fetched": 0,
        "proposals_created": 0,
        "duplicates_skipped": 0,
        "l1_rejected": 0,
        "proposal_ids": [],
        "window_since": _iso_utc(since),
        "window_until": _iso_utc(until),
        "summary": "",
    }

    with sessions().begin() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        if project_context.project_is_frozen(project_id):
            raise EvolutionFrozenError(project_context.ARCHIVED_DETAIL)
        active = (
            db.query(EvolutionRun)
            .filter(
                EvolutionRun.project_id == project_id,
                EvolutionRun.status == "running",
            )
            .first()
        )
        if active is not None:
            raise EvolutionConflictError("该项目已有正在进行的演化运行")
        if claim_scheduled_at is not None:
            evo_cfg = db.get(ProjectEvolutionConfig, project_id)
            if evo_cfg is not None:
                evo_cfg.last_triggered_at = _as_utc(claim_scheduled_at)
                evo_cfg.updated_at = now
        run = EvolutionRun(
            id=uuid.uuid4().hex,
            project_id=project_id,
            trigger_type=trigger_type,
            status="running",
            signal_summary=json.dumps(init_summary, ensure_ascii=False),
            created_by=created_by,
            started_at=now,
            created_at=now,
        )
        db.add(run)
        try:
            db.flush()
        except IntegrityError as exc:
            raise EvolutionConflictError("该项目已有正在进行的演化运行") from exc
        return serialize_run(run)


def execute_evolution_run(
    run_id: str,
    *,
    trace_source: Any | None = None,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Execute one TraceSource-based knowledge evolution run."""
    with sessions()() as db:
        run_row = db.get(EvolutionRun, run_id)
        if run_row is None:
            raise LookupError(f"Evolution run {run_id} not found")
        project_id = run_row.project_id
        created_by = run_row.created_by
        init_summary = _loads_json(run_row.signal_summary, {})
        if not isinstance(init_summary, dict):
            init_summary = {}
        evo_cfg = db.get(ProjectEvolutionConfig, project_id)
        prev_watermark = (
            _as_utc(evo_cfg.last_trace_timestamp)
            if evo_cfg is not None and evo_cfg.last_trace_timestamp is not None
            else None
        )
        cfg_dict = project_context.evolution_config(project_id, db)

    explicit_since = _parse_utc(init_summary.get("explicit_since"))
    explicit_until = _parse_utc(init_summary.get("explicit_until"))
    run_now = _as_utc(now) if now is not None else datetime.now(UTC)
    window_since = explicit_since if explicit_since is not None else prev_watermark
    window_until = explicit_until if explicit_until is not None else run_now
    effective_limit = int(
        init_summary.get("max_traces") or cfg_dict["max_traces_per_run"]
    )
    provider_override = init_summary.get("provider_id")
    model_override = init_summary.get("model")

    secrets: dict[str, str] = {}
    plugin_id: str | None = None
    try:
        if trace_source is not None:
            plugin = trace_source
            plugin_id = str(getattr(trace_source, "id", "custom"))
            params: dict[str, Any] = {}
            secrets = {}
        else:
            plugin, plugin_id, params, secrets = resolve_trace_source(project_id)

        query = TraceQuery(
            project_id=project_id,
            parameters=params,
            secrets=secrets,
            since=window_since,
            until=window_until,
            limit=effective_limit,
        )
        raw_conversations = _fetch_conversations(plugin, query)
        conversations = _filter_and_scrub_conversations(
            raw_conversations, project_id=project_id, secrets=secrets
        )
        traces_fetched = len(conversations)

        if traces_fetched == 0:
            next_wm = _compute_next_watermark(
                prev_watermark=prev_watermark,
                window_since=window_since,
                window_until=window_until,
                conversations=(),
                limit=effective_limit,
            )
            finished = datetime.now(UTC)
            summary_msg = "时间窗内未获取到新对话追踪记录，无需生成纠错提案。"
            summary_payload = {
                **init_summary,
                "trace_source_plugin": plugin_id,
                "traces_fetched": 0,
                "proposals_created": 0,
                "duplicates_skipped": 0,
                "l1_rejected": 0,
                "proposal_ids": [],
                "window_since": _iso_utc(window_since),
                "window_until": _iso_utc(window_until),
                "summary": summary_msg,
            }
            with sessions().begin() as db:
                run = db.get(EvolutionRun, run_id)
                if run is None:
                    raise LookupError(f"Evolution run {run_id} not found")
                run.status = "succeeded"
                run.stop_reason = "no_traces"
                run.error = None
                run.signal_summary = json.dumps(summary_payload, ensure_ascii=False)
                run.finished_at = finished
                if next_wm is not None:
                    cfg_row = db.get(ProjectEvolutionConfig, project_id)
                    if cfg_row is None:
                        db.add(
                            ProjectEvolutionConfig(
                                project_id=project_id,
                                enabled=False,
                                last_trace_timestamp=next_wm,
                                created_at=finished,
                                updated_at=finished,
                            )
                        )
                    else:
                        cfg_row.last_trace_timestamp = next_wm
                        cfg_row.updated_at = finished
                db.flush()
                return serialize_run(run)

        actor = Requester(
            id=created_by or "evolution-scheduler",
            role="admin",
            token="",
            project_id=project_id,
        )
        proposal_ids: list[str] = []
        duplicates_skipped = 0
        l1_rejected = 0
        chunk_size = max(1, int(batch_size))

        for start in range(0, traces_fetched, chunk_size):
            batch = conversations[start : start + chunk_size]
            description, raw_evidence, sql_blocks, trace_ids, message_summary = (
                _build_batch_inputs(
                    batch,
                    window_since=window_since,
                    window_until=window_until,
                )
            )
            try:
                synthesized = knowledge_synthesizer.synthesize_proposals(
                    project_id=project_id,
                    source_type="evolution",
                    description=description,
                    raw_evidence=raw_evidence,
                    sql_blocks=sql_blocks,
                    provider_id=provider_override,
                    model=model_override,
                    allow_multi=True,
                    skip_duplicates=False,
                    llm_fn=llm_fn,
                )
            except knowledge_synthesizer.LLMUnavailableError as exc:
                if exc.message == _EMPTY_PROPOSALS_MSG:
                    synthesized = []
                else:
                    raise

            for cand in synthesized:
                if cand.is_duplicate:
                    duplicates_skipped += 1
                    continue
                if not cand.gate_report.get("passed", False):
                    l1_rejected += 1
                    continue
                try:
                    created = _persist_evolution_proposal(
                        project_id=project_id,
                        run_id=run_id,
                        plugin_id=plugin_id,
                        actor=actor,
                        candidate=cand,
                        batch_trace_ids=trace_ids,
                        batch_session_ids=raw_evidence["session_ids"],
                        window_since=window_since,
                        window_until=window_until,
                        conversation_count=len(batch),
                        message_summary=message_summary,
                    )
                    proposal_ids.append(str(created["id"]))
                except GateValidationError:
                    l1_rejected += 1
                except ValueError as exc:
                    if "does not change" in str(exc):
                        duplicates_skipped += 1
                    else:
                        raise

        next_wm = _compute_next_watermark(
            prev_watermark=prev_watermark,
            window_since=window_since,
            window_until=window_until,
            conversations=conversations,
            limit=effective_limit,
        )
        summary_parts = [
            f"已分析 {traces_fetched} 条对话追踪记录",
            f"生成 {len(proposal_ids)} 条待审批纠错提案",
        ]
        if duplicates_skipped:
            summary_parts.append(f"跳过 {duplicates_skipped} 条重复提案")
        if l1_rejected:
            summary_parts.append(f"拦截 {l1_rejected} 条 L1 校验未通过提案")
        summary_msg = "，".join(summary_parts) + "。"

        finished = datetime.now(UTC)
        summary_payload = {
            **init_summary,
            "trace_source_plugin": plugin_id,
            "traces_fetched": traces_fetched,
            "proposals_created": len(proposal_ids),
            "duplicates_skipped": duplicates_skipped,
            "l1_rejected": l1_rejected,
            "proposal_ids": proposal_ids,
            "window_since": _iso_utc(window_since),
            "window_until": _iso_utc(window_until),
            "summary": summary_msg,
        }
        with sessions().begin() as db:
            run = db.get(EvolutionRun, run_id)
            if run is None:
                raise LookupError(f"Evolution run {run_id} not found")
            run.status = "succeeded"
            run.stop_reason = "completed"
            run.error = None
            run.proposal_id = proposal_ids[0] if proposal_ids else None
            run.signal_summary = json.dumps(summary_payload, ensure_ascii=False)
            run.finished_at = finished
            if next_wm is not None:
                cfg_row = db.get(ProjectEvolutionConfig, project_id)
                if cfg_row is None:
                    db.add(
                        ProjectEvolutionConfig(
                            project_id=project_id,
                            enabled=False,
                            last_trace_timestamp=next_wm,
                            created_at=finished,
                            updated_at=finished,
                        )
                    )
                else:
                    cfg_row.last_trace_timestamp = next_wm
                    cfg_row.updated_at = finished
            db.flush()
            return serialize_run(run)

    except EvolutionConfigError as exc:
        return _mark_run_failed(
            run_id,
            stop_reason="config_error",
            error=_scrub_error(exc, secrets),
            extra_summary={
                "trace_source_plugin": plugin_id,
                "window_since": _iso_utc(window_since),
                "window_until": _iso_utc(window_until),
            },
        )
    except knowledge_synthesizer.LLMUnavailableError as exc:
        return _mark_run_failed(
            run_id,
            stop_reason="llm_unavailable",
            error=_scrub_error(exc.message, secrets),
            extra_summary={
                "trace_source_plugin": plugin_id,
                "window_since": _iso_utc(window_since),
                "window_until": _iso_utc(window_until),
            },
        )
    except Exception as exc:  # noqa: BLE001 - persist scrubbed run failure instead of leaving run stuck running
        logger.warning(
            "Evolution run %s failed: %s",
            run_id,
            type(exc).__name__,
        )
        return _mark_run_failed(
            run_id,
            stop_reason="error",
            error=_scrub_error(exc, secrets),
            extra_summary={
                "trace_source_plugin": plugin_id,
                "window_since": _iso_utc(window_since),
                "window_until": _iso_utc(window_until),
            },
        )


def run_evolution(
    project_id: str = project_context.DEFAULT_PROJECT_ID,
    *,
    trigger_type: str = "manual",
    created_by: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    max_traces: int | None = None,
    provider_id: str | None = None,
    model: str | None = None,
    trace_source: Any | None = None,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Synchronously reserve and execute one full evolution run."""
    created = create_evolution_run(
        project_id,
        trigger_type=trigger_type,
        created_by=created_by,
        since=since,
        until=until,
        max_traces=max_traces,
        provider_id=provider_id,
        model=model,
    )
    return execute_evolution_run(
        created["id"],
        trace_source=trace_source,
        llm_fn=llm_fn,
        now=now,
        batch_size=batch_size,
    )
