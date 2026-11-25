"""Unified LLM knowledge synthesizer with project context retrieval, routing, deduplication, and L1 validation."""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.fernet import InvalidToken

from . import knowledge, project_context, provider_probe
from .agent_profile_api import _catalog_set
from .agent_types import ProviderConfig
from .assistant_catalog import is_catalog_governed, resolve_system_default_llm
from .crypto import decrypt
from .db import (
    CorrectionProposal,
    ProjectEvolutionConfig,
    Provider,
    Setting,
    sessions,
)
from .eval_questions import extract_tables_and_fields
from .markdown import doc_ref
from .semantic_overrides import (
    OVERRIDE_RELATIVE_PATH,
    dump_overrides_yaml,
    load_overrides_file,
    merge_overrides_doc,
    normalize_structured_patch,
    override_file_path,
    run_l1_gate,
    scrub_untrusted_text,
    validate_markdown_relpath,
)
from .storage_errors import StorageUnavailable

logger = logging.getLogger(__name__)

TargetKind = Literal["document", "override"]
ProposalSource = Literal["manual", "mcp", "evolution", "discovery"]

_MAX_CONTEXT_TABLES = 20
_MAX_CONTEXT_DOCS = 10
_MAX_DOC_CHARS = 3000
_LLM_TIMEOUT_SECONDS = 30.0
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", re.IGNORECASE)


class LLMUnavailableError(RuntimeError):
    """Raised when no LLM provider/model is configured or the LLM output is unusable."""

    def __init__(
        self,
        message: str = "未配置可用大模型或模型调用失败，请先配置模型或切换为手工编辑",
        *,
        code: str = "llm_unavailable",
    ) -> None:
        self.code = code
        self.message = message
        self.manual_edit_recommended = True
        super().__init__(message)


@dataclass(frozen=True)
class DuplicateCheckResult:
    """Result of comparing a candidate proposal against existing knowledge and pending queue."""

    is_duplicate: bool
    duplicate_of: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SynthesizedProposal:
    """Structured knowledge evolution / correction candidate ready for preview or persistence."""

    target: str
    target_kind: TargetKind
    resolved_path: str
    is_new_file: bool
    patch: str
    structured_patch: dict[str, Any] | None
    diff: str
    reason: str
    gate_report: dict[str, Any]
    used_llm: bool
    source_generated: bool = False
    caliber: str | None = None
    reference_sql: str | None = None
    is_duplicate: bool = False
    duplicate_of: str | None = None
    duplicate_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def markdown_root_for_project(project_id: str) -> str:
    """Return the configured relative Markdown root directory for a project."""
    try:
        cfg = project_context.plugin_config("source", "markdown", project_id)
    except Exception:  # noqa: BLE001
        return "knowledge"
    if cfg and isinstance(cfg[1], Mapping):
        raw = str(cfg[1].get("path") or "knowledge").strip().strip("/")
        if raw:
            return raw
    return "knowledge"


def _project_type_for(project_id: str) -> str:
    try:
        return project_context.project_type(project_id)
    except Exception:  # noqa: BLE001
        return project_context.DEFAULT_PROJECT_TYPE


def unified_diff_text(
    path: str, old: str, new: str, *, is_new_file: bool = False
) -> str:
    """Render a unified diff, using ``/dev/null -> b/<path>`` for newly created files."""
    from_file = "/dev/null" if is_new_file else path
    to_file = f"b/{path}" if is_new_file else path
    old_lines = [] if is_new_file else old.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_lines,
            new.splitlines(keepends=True),
            fromfile=from_file,
            tofile=to_file,
        )
    )


def _configured_system_setting(db, key: str, env_key: str) -> str | None:
    row = db.get(Setting, key)
    value = row.value.strip() if row is not None and row.value else ""
    if value:
        return value
    env_val = os.environ.get(env_key, "").strip()
    return env_val or None


def resolve_llm_provider(
    project_id: str,
    *,
    source_type: str = "manual",
    provider_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Resolve the active LLM provider/model from explicit override, project config, or system default."""
    explicit_pid = provider_id.strip() if isinstance(provider_id, str) else None
    explicit_model = model.strip() if isinstance(model, str) else None

    with sessions()() as db:
        configured_pid: str | None = explicit_pid or None
        configured_model: str | None = explicit_model or None

        if not configured_pid:
            if source_type == "evolution":
                evo_row = db.get(ProjectEvolutionConfig, project_id)
                if evo_row is not None:
                    configured_pid = (evo_row.provider_id or "").strip() or None
                    configured_model = (
                        configured_model or (evo_row.model or "").strip() or None
                    )
                if not configured_pid:
                    evo_cfg = project_context.evolution_config(project_id, db)
                    configured_pid = evo_cfg.get("evolution_provider_id")
                    configured_model = configured_model or evo_cfg.get(
                        "evolution_model"
                    )
            elif source_type == "discovery":
                disc_cfg = project_context.discovery_config(project_id, db)
                configured_pid = disc_cfg.get("provider_id")
                configured_model = configured_model or disc_cfg.get("model")

        system_pid = _configured_system_setting(
            db, "assistant_provider_id", "DATAAGENT_ASK_PROVIDER_ID"
        )
        system_model = _configured_system_setting(
            db, "assistant_model", "DATAAGENT_ASK_MODEL"
        )
        governed = is_catalog_governed(db)

        row: Provider | None = None
        used_fallback_provider = False
        chosen_model: str | None = None
        if configured_pid:
            row = db.get(Provider, configured_pid)
            if row is None and explicit_pid:
                return None
        if row is None and not explicit_pid:
            if governed:
                resolved_sys = resolve_system_default_llm(db)
                if resolved_sys is None:
                    return None
                row, chosen_model = resolved_sys
            else:
                if system_pid:
                    row = db.get(Provider, system_pid)
                if row is None:
                    row = (
                        db.query(Provider).order_by(Provider.name, Provider.id).first()
                    )
                    used_fallback_provider = row is not None

        if row is None or not row.base_url:
            return None

        try:
            models = [
                str(m).strip() for m in json.loads(row.models or "[]") if str(m).strip()
            ]
        except ValueError:
            models = []

        catalog = _catalog_set(row)
        if chosen_model is None:
            if configured_model:
                if catalog is not None and configured_model not in catalog:
                    return None
                chosen_model = configured_model
            elif catalog is not None:
                resolved_sys = resolve_system_default_llm(db)
                if (
                    resolved_sys is not None
                    and resolved_sys[0].id == row.id
                    and resolved_sys[1] in catalog
                ):
                    chosen_model = resolved_sys[1]
                else:
                    chosen_model = next((m for m in models if m in catalog), "")
            elif system_model and (not models or system_model in models):
                chosen_model = system_model
            elif used_fallback_provider:
                chosen_model = models[0] if models else ""
            else:
                chosen_model = models[0] if models else ""
        if not chosen_model:
            return None

        api_key = ""
        if row.api_key:
            try:
                api_key = decrypt(row.api_key)
            except (InvalidToken, ValueError):
                api_key = ""

        return {
            "id": row.id,
            "name": row.name,
            "type": row.type,
            "base_url": row.base_url,
            "model": chosen_model,
            "api_key": api_key,
        }


def _build_system_prompt(allow_multi: bool) -> str:
    base = (
        "你是企业级数仓语义知识库演进与纠错专家。请根据输入的脱敏上下文（项目类型、表与字段元数据、"
        "已有知识文档列表与摘要、已有语义覆盖、以及触发输入）归纳高质量知识变更，并严格输出纯 JSON 对象（禁止输出 Markdown 代码块以外的解释）。\n"
        "路由规则（必须严格遵守）：\n"
        "1. 若项目类型为 warehouse，且变更属于单表/单字段说明、同义词、使用约束、外键关系(relationship)、或单一指标口径(metric)，"
        '路由到 structured_patch（目标为 overrides/semantic_overrides.yaml），设置 route="override"。\n'
        '   - structured_patch 必须符合 semantic_overrides v1 schema：{"version": 1, "tables"|"fields"|"relationships"|"metrics": {...}}。\n'
        "   - 表覆盖必填 database, name 及至少一项 comment/synonyms/instructions；字段覆盖必填 database, table, name 及至少一项 comment/synonyms/instructions。\n"
        "   - 外键覆盖必填 name, from_database, from_table, from_columns, to_database, to_table, to_columns。\n"
        "   - 指标覆盖必填 name, expression（以及推荐 dialect, tables, filters, description）。\n"
        "2. 若变更属于跨表业务规则、专题口径说明、常见问题排查或文档型项目知识，优先合并进已有最匹配的 Markdown 文档对应章节："
        '设置 route="existing_document"，target 为该文档的 ref（不可选择 generated=true 的派生文档），并在 patch 中输出整合后的完整 Markdown 文档全文。\n'
        "3. 若确实没有匹配的已有非派生 Markdown 文档且不适合单表/字段/指标覆盖，建议新建 Markdown 文档："
        '设置 route="new_document"，new_doc_path 为相对路径 `<markdown_root>/<category>/<slug>.md`（禁止路径穿越、隐藏文件、非 .md 后缀及顶层目录），'
        "target 为 `document:<new_doc_path>`，并在 patch 中输出规范的完整 Markdown 内容。\n"
        "4. 若 caller 已显式指定可编辑目标 target，则遵守该目标类型输出对应 patch 或 structured_patch。\n"
    )
    if allow_multi:
        return (
            base
            + '输出格式：{"proposals": [{"route": "override|existing_document|new_document", "target": "...", "reason": "...", "structured_patch": {...}?, "patch": "..."?, "new_doc_path": "..."?, "reference_sql": "..."?}]}'
        )
    return (
        base
        + '输出格式：{"route": "override|existing_document|new_document", "target": "...", "reason": "...", "structured_patch": {...}?, "patch": "..."?, "new_doc_path": "..."?, "reference_sql": "..."?}'
    )


def generate_with_llm(context: dict[str, Any]) -> dict[str, Any] | None:
    """Invoke the configured LLM provider to synthesize structured/Markdown knowledge proposals."""
    provider_cfg = context.get("provider")
    if not isinstance(provider_cfg, Mapping) or not provider_cfg.get("base_url"):
        return None
    base_url = str(provider_cfg["base_url"]).rstrip("/")
    provider_type = str(provider_cfg.get("type") or "openai-completions")
    if provider_type == "openai-chat":
        provider_type = "openai-completions"
    model = str(provider_cfg.get("model") or "").strip()
    api_key = str(provider_cfg.get("api_key") or "")
    if not model:
        return None

    allow_multi = bool(context.get("allow_multi"))
    system_prompt = _build_system_prompt(allow_multi)
    user_payload = json.dumps(
        {k: v for k, v in context.items() if k != "provider"},
        ensure_ascii=False,
    )

    provider_obj = ProviderConfig(
        id=str(provider_cfg.get("id") or "synth"),
        name=str(provider_cfg.get("name") or "synth"),
        type=provider_type,
        base_url=base_url,
        models=[model],
        api_key=api_key or None,
    )
    headers: dict[str, str] = {"Content-Type": "application/json"}
    try:
        if provider_type == "anthropic-messages":
            headers["anthropic-version"] = "2023-06-01"
            if api_key:
                headers["x-api-key"] = api_key
            url = f"{base_url}/v1/messages"
            body: dict[str, Any] = {
                "model": model,
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_payload}],
            }
        elif provider_type == "openai-responses":
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            url = f"{base_url}/responses"
            body = {
                "model": model,
                "instructions": system_prompt,
                "input": [{"role": "user", "content": user_payload}],
                "store": False,
                "stream": False,
            }
        elif provider_type == "google-generative-ai":
            if api_key:
                headers["x-goog-api-key"] = api_key
            resource = model if model.startswith("models/") else f"models/{model}"
            url = f"{base_url}/{resource}:generateContent"
            body = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_payload}]}],
            }
        else:
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            url = f"{base_url}/chat/completions"
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
            }

        resp = httpx.post(
            url,
            headers=headers,
            json=body,
            timeout=_LLM_TIMEOUT_SECONDS,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json()
        text = provider_probe._response_text(provider_obj, data)
        if not text and isinstance(data, Mapping):
            choices = data.get("choices")
            if (
                isinstance(choices, list)
                and choices
                and isinstance(choices[0], Mapping)
            ):
                msg = choices[0].get("message")
                if isinstance(msg, Mapping) and isinstance(msg.get("content"), str):
                    text = msg["content"]
        return provider_probe.parse_json_object(text)
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Knowledge synthesizer LLM call failed: %s", type(exc).__name__)
        return None


def fetch_node(
    values: Mapping[str, Any], generation: str, ref: str
) -> dict[str, Any] | None:
    """Fetch one entity or RELATES_TO edge from the active Neo4j generation."""
    try:
        if ref.startswith("relationship:"):
            rel_rows = knowledge.execute(
                knowledge.read_graph(values),
                (
                    "MATCH (a:SDATable {generation:$generation})-[r:RELATES_TO {ref:$ref}]->(b:SDATable {generation:$generation}) "
                    "RETURN {ref: r.ref, name: r.name, description: r.description, instructions: r.instructions, "
                    "from_catalog: a.catalog, from_database: a.database, from_table: a.name, from_columns: r.from_columns, "
                    "to_catalog: b.catalog, to_database: b.database, to_table: b.name, to_columns: r.to_columns} AS node"
                ),
                {"generation": generation, "ref": ref},
            )
            if rel_rows and isinstance(rel_rows[0].get("node"), dict):
                return dict(rel_rows[0]["node"])
        rows = knowledge.execute(
            knowledge.read_graph(values),
            "MATCH (n:SDAEntity {generation:$generation, ref:$ref}) RETURN properties(n) AS node",
            {"generation": generation, "ref": ref},
        )
    except (KeyError, StorageUnavailable, ValueError, RuntimeError):
        return None
    if len(rows) == 1 and isinstance(rows[0].get("node"), dict):
        return dict(rows[0]["node"])
    return None


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "") if m.group(0)}


def _list_graph_documents(
    values: Mapping[str, Any], generation: str
) -> list[dict[str, Any]]:
    try:
        rows = knowledge.execute(
            knowledge.read_graph(values),
            (
                "MATCH (d:SDADocument {generation:$generation}) "
                "WHERE coalesce(d.generated, false) = false "
                "RETURN d.ref AS ref, d.category AS category, d.path AS path, "
                "d.source_file AS source_file, d.title AS title, d.content AS content"
            ),
            {"generation": generation},
        )
    except (KeyError, StorageUnavailable, ValueError, RuntimeError, OSError):
        return []
    docs: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        source_file = str(row.get("source_file") or "").strip()
        path = str(row.get("path") or "").strip()
        category = str(row.get("category") or "").strip()
        if path == "index.md" or source_file.startswith("ossie/"):
            continue
        ref = str(row.get("ref") or "").strip()
        if not ref and category and path:
            ref = doc_ref(category, path)
        if not ref:
            continue
        docs.append(
            {
                "ref": ref,
                "category": category,
                "path": path,
                "source_file": source_file,
                "title": str(row.get("title") or "").strip(),
                "content": str(row.get("content") or ""),
            }
        )
    return docs


def _list_disk_documents(
    source_dir: Path | None, markdown_root: str
) -> list[dict[str, Any]]:
    if source_dir is None or not source_dir.is_dir():
        return []
    root_dir = source_dir / markdown_root
    if not root_dir.is_dir() or root_dir.is_symlink():
        return []
    docs: list[dict[str, Any]] = []
    root_resolved = source_dir.resolve()
    for category_dir in sorted(
        p
        for p in root_dir.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
    ):
        for md_path in sorted(category_dir.rglob("*.md")):
            if not md_path.is_file() or md_path.is_symlink():
                continue
            try:
                if not md_path.resolve().is_relative_to(root_resolved):
                    continue
                content = md_path.read_text(encoding="utf-8")
            except OSError:
                continue
            rel_in_cat = md_path.relative_to(category_dir).as_posix()
            if rel_in_cat == "index.md":
                continue
            rel_source = md_path.relative_to(source_dir).as_posix()
            title = next(
                (
                    line[2:].strip()
                    for line in content.splitlines()
                    if line.startswith("# ")
                ),
                rel_in_cat,
            )
            docs.append(
                {
                    "ref": doc_ref(category_dir.name, rel_in_cat),
                    "category": category_dir.name,
                    "path": rel_in_cat,
                    "source_file": rel_source,
                    "title": title,
                    "content": content,
                }
            )
    return docs


def _collect_documents(
    values: Mapping[str, Any],
    generation: str,
    source_dir: Path | None,
    markdown_root: str,
) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}
    for doc in _list_graph_documents(values, generation):
        key = doc.get("source_file") or doc["ref"]
        by_source[key] = doc
    for doc in _list_disk_documents(source_dir, markdown_root):
        key = doc.get("source_file") or doc["ref"]
        if key in by_source:
            existing = dict(by_source[key])
            existing["content"] = doc["content"]
            existing["source_file"] = doc["source_file"]
            by_source[key] = existing
        else:
            by_source[key] = doc
    return list(by_source.values())


def _safe_search_refs(project_id: str, query_text: str) -> set[str]:
    if not query_text.strip():
        return set()
    try:
        hits = knowledge.search(
            query_text[:500], "all", top_k=10, project_id=project_id
        )
    except Exception:  # noqa: BLE001
        return set()
    return {
        str(item.get("ref") or "").strip()
        for item in hits
        if isinstance(item, Mapping) and item.get("ref")
    }


def _compact_table(table: Mapping[str, Any]) -> dict[str, Any]:
    fields = [
        {
            "name": str(f.get("name") or ""),
            "type": str(f.get("type") or ""),
            "comment": scrub_untrusted_text(str(f.get("comment") or ""))[:200],
            **({"synonyms": list(f["synonyms"])} if f.get("synonyms") else {}),
            **(
                {"instructions": scrub_untrusted_text(str(f["instructions"]))[:200]}
                if f.get("instructions")
                else {}
            ),
        }
        for f in (table.get("fields") or [])[:60]
        if isinstance(f, Mapping) and f.get("name")
    ]
    item: dict[str, Any] = {
        "database": str(table.get("database") or ""),
        "name": str(table.get("name") or ""),
        "comment": scrub_untrusted_text(str(table.get("comment") or ""))[:300],
        "fields": fields,
    }
    if table.get("catalog"):
        item["catalog"] = str(table["catalog"])
    if table.get("synonyms"):
        item["synonyms"] = list(table["synonyms"])
    if table.get("instructions"):
        item["instructions"] = scrub_untrusted_text(str(table["instructions"]))[:300]
    if table.get("relationships"):
        item["relationships"] = list(table["relationships"])[:15]
    return item


def collect_project_context(
    project_id: str,
    *,
    query_text: str = "",
    target: str | None = None,
    sql_blocks: Sequence[Mapping[str, Any]] = (),
    max_tables: int = _MAX_CONTEXT_TABLES,
    max_docs: int = _MAX_CONTEXT_DOCS,
) -> dict[str, Any]:
    """Retrieve relevant tables, fields, Markdown documents, overrides, and pending proposals."""
    values = knowledge.settings(project_id)
    project = project_context.from_values(values, project_id)
    proj_type = _project_type_for(project_id)
    md_root = markdown_root_for_project(project_id)
    src_dir = (
        Path(project.source_directory_value) if project.source_directory_value else None
    )

    sql_tables: set[str] = set()
    for block in sql_blocks:
        if isinstance(block, Mapping) and str(block.get("sql") or "").strip():
            try:
                tbls, _ = extract_tables_and_fields(
                    str(block["sql"]), str(block.get("dialect") or "hive")
                )
                sql_tables.update(t.lower() for t in tbls)
            except ValueError:
                continue

    search_refs = _safe_search_refs(project_id, query_text)
    query_tokens = _tokens(query_text + " " + (target or ""))

    all_docs = _collect_documents(values, project.generation, src_dir, md_root)
    scored_docs: list[tuple[int, dict[str, Any]]] = []
    for doc in all_docs:
        score = 0
        if target and (
            target == doc["ref"] or target == f"document:{doc.get('source_file')}"
        ):
            score += 100
        if doc["ref"] in search_refs:
            score += 20
        doc_tokens = _tokens(
            f"{doc.get('title', '')} {doc.get('category', '')} {doc.get('path', '')} {doc.get('content', '')[:1000]}"
        )
        score += len(query_tokens & doc_tokens)
        scored_docs.append((score, doc))
    scored_docs.sort(key=lambda pair: (-pair[0], pair[1].get("source_file", "")))
    selected_docs = [doc for _, doc in scored_docs[:max_docs]]

    selected_tables: list[dict[str, Any]] = []
    if proj_type == "warehouse":
        try:
            described = knowledge.describe(summary=False, project_id=project_id)
        except (
            KeyError,
            StorageUnavailable,
            ValueError,
            RuntimeError,
            ConnectionError,
            OSError,
        ):
            described = []
        scored_tables: list[tuple[int, dict[str, Any]]] = []
        for tbl in described:
            if not isinstance(tbl, Mapping):
                continue
            db_name = str(tbl.get("database") or "")
            t_name = str(tbl.get("name") or "")
            cat = str(tbl.get("catalog") or "")
            full_no_cat = f"{db_name}.{t_name}".lower()
            full_cat = f"{cat}.{db_name}.{t_name}".lower() if cat else ""
            t_ref = f"table:{full_cat or full_no_cat}"
            score = 0
            if target and (
                target == t_ref
                or target == f"table:{full_no_cat}"
                or target.startswith(f"field:{full_no_cat}.")
                or (full_cat and target.startswith(f"field:{full_cat}."))
                or full_no_cat in target.lower()
            ):
                score += 100
            if full_no_cat in sql_tables or (full_cat and full_cat in sql_tables):
                score += 50
            if t_ref in search_refs or f"table:{full_no_cat}" in search_refs:
                score += 20
            field_names = " ".join(
                f"{f.get('name', '')} {f.get('comment', '')}"
                for f in (tbl.get("fields") or [])
                if isinstance(f, Mapping)
            )
            tbl_tokens = _tokens(
                f"{db_name} {t_name} {tbl.get('comment', '')} {field_names}"
            )
            score += len(query_tokens & tbl_tokens)
            scored_tables.append((score, dict(tbl)))
        scored_tables.sort(
            key=lambda pair: (
                -pair[0],
                str(pair[1].get("database") or ""),
                str(pair[1].get("name") or ""),
            )
        )
        selected_tables = [_compact_table(tbl) for _, tbl in scored_tables[:max_tables]]

    overrides_doc: dict[str, Any] = {"version": 1}
    if src_dir is not None and src_dir.is_dir():
        try:
            overrides_doc, _ = load_overrides_file(src_dir)
        except ValueError:
            overrides_doc = {"version": 1}

    categories = sorted(
        {
            str(d.get("category") or "").strip()
            for d in all_docs
            if str(d.get("category") or "").strip()
        }
    )

    return {
        "project_id": project_id,
        "project_type": proj_type,
        "generation": project.generation,
        "markdown_root": md_root,
        "source_dir": src_dir,
        "categories": categories,
        "tables": selected_tables,
        "documents": [
            {
                "ref": d["ref"],
                "category": d["category"],
                "path": d["path"],
                "source_file": d["source_file"],
                "title": scrub_untrusted_text(d["title"]),
                "content": scrub_untrusted_text(d["content"][:_MAX_DOC_CHARS]),
            }
            for d in selected_docs
        ],
        "all_documents": all_docs,
        "existing_overrides": overrides_doc,
        "values": values,
    }


def _normalize_text_for_dup(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _jaccard_overlap(a: str, b: str) -> float:
    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _extract_added_lines(old_text: str, new_text: str) -> str:
    old_set = {line.strip() for line in old_text.splitlines() if line.strip()}
    added = [
        line.strip()
        for line in new_text.splitlines()
        if line.strip() and line.strip() not in old_set
    ]
    return "\n".join(added)


def _norm_rel_pair(
    entry: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], str, tuple[str, ...]] | None:
    fdb = str(entry.get("from_database") or "").strip().lower()
    ftbl = str(entry.get("from_table") or "").strip().lower()
    tdb = str(entry.get("to_database") or "").strip().lower()
    ttbl = str(entry.get("to_table") or "").strip().lower()
    fcols = tuple(
        str(c).strip().lower()
        for c in (entry.get("from_columns") or ())
        if str(c).strip()
    )
    tcols = tuple(
        str(c).strip().lower()
        for c in (entry.get("to_columns") or ())
        if str(c).strip()
    )
    if not (fdb and ftbl and tdb and ttbl and fcols and tcols):
        return None
    return (f"{fdb}.{ftbl}", fcols, f"{tdb}.{ttbl}", tcols)


def _override_matches_existing(
    existing_doc: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    for section in ("tables", "fields"):
        cand_sec = candidate.get(section) or {}
        exist_sec = existing_doc.get(section) or {}
        for key, entry in cand_sec.items():
            if key not in exist_sec:
                return False
            existing_entry = exist_sec[key]
            if not isinstance(existing_entry, Mapping):
                return False
            for attr in ("comment", "instructions"):
                val = str(entry.get(attr) or "").strip()
                if val and _normalize_text_for_dup(val) not in _normalize_text_for_dup(
                    str(existing_entry.get(attr) or "")
                ):
                    return False
            cand_syn = {
                str(s).strip().lower()
                for s in (entry.get("synonyms") or ())
                if str(s).strip()
            }
            exist_syn = {
                str(s).strip().lower()
                for s in (existing_entry.get("synonyms") or ())
                if str(s).strip()
            }
            if cand_syn and not cand_syn.issubset(exist_syn):
                return False
    for rkey, rentry in (candidate.get("relationships") or {}).items():
        pair = _norm_rel_pair(rentry)
        existing_pairs = {
            _norm_rel_pair(v)
            for v in (existing_doc.get("relationships") or {}).values()
            if isinstance(v, Mapping)
        }
        if (
            rkey not in (existing_doc.get("relationships") or {})
            and pair not in existing_pairs
        ):
            return False
    for mkey, mentry in (candidate.get("metrics") or {}).items():
        exist_metrics = existing_doc.get("metrics") or {}
        if mkey not in exist_metrics:
            return False
        em = exist_metrics[mkey]
        if _normalize_text_for_dup(
            str(mentry.get("expression") or "")
        ) != _normalize_text_for_dup(str(em.get("expression") or "")):
            return False
        for attr in ("description", "instructions", "dialect"):
            val = _normalize_text_for_dup(str(mentry.get(attr) or ""))
            if val and val != _normalize_text_for_dup(str(em.get(attr) or "")):
                return False
        cand_filters = sorted(
            _normalize_text_for_dup(str(f))
            for f in (mentry.get("filters") or ())
            if str(f).strip()
        )
        exist_filters = sorted(
            _normalize_text_for_dup(str(f))
            for f in (em.get("filters") or ())
            if str(f).strip()
        )
        if cand_filters != exist_filters:
            return False
        cand_tables = {
            str(t).strip().lower()
            for t in (
                ([mentry["table"]] if mentry.get("table") else [])
                + list(mentry.get("tables") or ())
            )
            if str(t).strip()
        }
        exist_tables = {
            str(t).strip().lower()
            for t in (
                ([em["table"]] if em.get("table") else [])
                + list(em.get("tables") or ())
            )
            if str(t).strip()
        }
        if cand_tables != exist_tables:
            return False
        cand_syn = {
            str(s).strip().lower()
            for s in (mentry.get("synonyms") or ())
            if str(s).strip()
        }
        exist_syn = {
            str(s).strip().lower() for s in (em.get("synonyms") or ()) if str(s).strip()
        }
        if cand_syn and not cand_syn.issubset(exist_syn):
            return False
    return True


def _overrides_overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(
        b, sort_keys=True, ensure_ascii=False
    ):
        return True
    for r_a in (a.get("relationships") or {}).values():
        if isinstance(r_a, Mapping):
            pair_a = _norm_rel_pair(r_a)
            for r_b in (b.get("relationships") or {}).values():
                if (
                    isinstance(r_b, Mapping)
                    and pair_a
                    and pair_a == _norm_rel_pair(r_b)
                ):
                    return True
    for mkey_a, m_a in (a.get("metrics") or {}).items():
        m_b_map = b.get("metrics") or {}
        if mkey_a in m_b_map:
            return True
        expr_a = _normalize_text_for_dup(str(m_a.get("expression") or ""))
        for m_b in m_b_map.values():
            if expr_a and expr_a == _normalize_text_for_dup(
                str(m_b.get("expression") or "")
            ):
                return True
    for section in ("tables", "fields"):
        sec_a = a.get(section) or {}
        sec_b = b.get(section) or {}
        for key in sec_a:
            if key in sec_b:
                ea, eb = sec_a[key], sec_b[key]
                text_a = _normalize_text_for_dup(
                    f"{ea.get('comment', '')} {ea.get('instructions', '')} {' '.join(ea.get('synonyms') or ())}"
                )
                text_b = _normalize_text_for_dup(
                    f"{eb.get('comment', '')} {eb.get('instructions', '')} {' '.join(eb.get('synonyms') or ())}"
                )
                if text_a == text_b or _jaccard_overlap(text_a, text_b) >= 0.8:
                    return True
    return False


def check_duplicate_candidate(
    project_id: str,
    *,
    target: str,
    target_kind: TargetKind,
    resolved_path: str,
    patch: str,
    structured_patch: dict[str, Any] | None,
    reason: str,
    original: str = "",
    is_new_file: bool = False,
    existing_overrides: Mapping[str, Any] | None = None,
    existing_docs: Sequence[Mapping[str, Any]] = (),
) -> DuplicateCheckResult:
    """Detect whether a candidate proposal duplicates existing knowledge or a pending proposal."""
    if target_kind == "override" and structured_patch is not None:
        if existing_overrides is not None:
            base_doc = dict(existing_overrides)
        else:
            src_dir_val = project_context.source_directory_value(project_id)
            if src_dir_val and Path(src_dir_val).is_dir():
                try:
                    base_doc, _ = load_overrides_file(Path(src_dir_val))
                except ValueError:
                    base_doc = {
                        "version": 1,
                        "tables": {},
                        "fields": {},
                        "relationships": {},
                        "metrics": {},
                    }
            else:
                base_doc = {
                    "version": 1,
                    "tables": {},
                    "fields": {},
                    "relationships": {},
                    "metrics": {},
                }
        try:
            current_yaml = dump_overrides_yaml(base_doc)
            merged_yaml = dump_overrides_yaml(
                merge_overrides_doc(base_doc, structured_patch)
            )
            if current_yaml == merged_yaml or _override_matches_existing(
                base_doc, structured_patch
            ):
                return DuplicateCheckResult(
                    True,
                    duplicate_of="existing_knowledge",
                    reason="该语义覆盖内容已存在于当前 semantic_overrides.yaml 中",
                )
        except ValueError:
            pass

    if target_kind == "document":
        norm_patch = _normalize_text_for_dup(patch)
        norm_orig = _normalize_text_for_dup(original)
        if not is_new_file and norm_patch and norm_patch == norm_orig:
            return DuplicateCheckResult(
                True,
                duplicate_of="existing_knowledge",
                reason="文档内容与现有知识文档完全一致，无新增变化",
            )
        if not is_new_file and original:
            added = _extract_added_lines(original, patch)
            if added and (
                _normalize_text_for_dup(added) in norm_orig
                or _jaccard_overlap(added, original) >= 0.9
            ):
                return DuplicateCheckResult(
                    True,
                    duplicate_of="existing_knowledge",
                    reason="新增规则说明已包含在当前文档中",
                )
        if is_new_file:
            for doc in existing_docs:
                doc_src = str(doc.get("source_file") or "")
                doc_content = str(doc.get("content") or "")
                if doc_src == resolved_path:
                    return DuplicateCheckResult(
                        True,
                        duplicate_of="existing_knowledge",
                        reason=f"目标文档 {resolved_path} 已存在",
                    )
                if doc_content and (
                    norm_patch == _normalize_text_for_dup(doc_content)
                    or SequenceMatcher(
                        None, norm_patch, _normalize_text_for_dup(doc_content)
                    ).ratio()
                    >= 0.92
                ):
                    return DuplicateCheckResult(
                        True,
                        duplicate_of="existing_knowledge",
                        reason=f"建议新建文档内容与已有文档 {doc_src or doc.get('ref')} 高度重复",
                    )

    norm_reason = _normalize_text_for_dup(reason)
    norm_patch_fp = hashlib.sha256(
        _normalize_text_for_dup(patch).encode("utf-8")
    ).hexdigest()
    with sessions()() as db:
        open_props = (
            db.query(CorrectionProposal)
            .filter(
                CorrectionProposal.project_id == project_id,
                CorrectionProposal.status.in_(("pending", "applying")),
            )
            .order_by(CorrectionProposal.created_at.desc())
            .all()
        )
        for prop in open_props:
            prop_kind = prop.target_kind or "document"
            if (
                target_kind == "override"
                and prop_kind == "override"
                and structured_patch is not None
            ):
                try:
                    other_sp = (
                        json.loads(prop.structured_patch)
                        if prop.structured_patch
                        else None
                    )
                except ValueError:
                    other_sp = None
                if isinstance(other_sp, Mapping) and _overrides_overlap(
                    structured_patch, other_sp
                ):
                    return DuplicateCheckResult(
                        True,
                        duplicate_of=f"pending_proposal:{prop.id}",
                        reason=f"与待审批提案 {prop.id} 的结构化覆盖重叠",
                    )
            if target_kind == "document" and prop_kind == "document":
                other_fp = hashlib.sha256(
                    _normalize_text_for_dup(prop.patch).encode("utf-8")
                ).hexdigest()
                same_target = prop.target == target or (
                    prop.source_path and prop.source_path == resolved_path
                )
                if same_target and (
                    other_fp == norm_patch_fp
                    or (
                        norm_reason
                        and _jaccard_overlap(
                            norm_reason, _normalize_text_for_dup(prop.reason)
                        )
                        >= 0.8
                    )
                    or SequenceMatcher(
                        None,
                        _normalize_text_for_dup(patch),
                        _normalize_text_for_dup(prop.patch),
                    ).ratio()
                    >= 0.9
                ):
                    return DuplicateCheckResult(
                        True,
                        duplicate_of=f"pending_proposal:{prop.id}",
                        reason=f"与待审批文档提案 {prop.id} 内容或目标重复",
                    )
                if not same_target and (
                    other_fp == norm_patch_fp
                    or (
                        _jaccard_overlap(patch, prop.patch) >= 0.85
                        and _jaccard_overlap(reason, prop.reason) >= 0.8
                    )
                ):
                    return DuplicateCheckResult(
                        True,
                        duplicate_of=f"pending_proposal:{prop.id}",
                        reason=f"与待审批文档提案 {prop.id} 语义高度重复",
                    )
    return DuplicateCheckResult(False)


def _match_existing_doc(
    candidate_target: str, docs: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    cand = candidate_target.strip()
    if not cand:
        return None
    for doc in docs:
        if cand in {
            doc.get("ref"),
            doc.get("source_file"),
            f"document:{doc.get('source_file')}",
        }:
            return dict(doc)
    return None


def _derive_override_target(structured_patch: Mapping[str, Any]) -> str | None:
    for prefix, section in (
        ("field", "fields"),
        ("table", "tables"),
        ("relationship", "relationships"),
        ("metric", "metrics"),
    ):
        entries = structured_patch.get(section)
        if isinstance(entries, Mapping) and entries:
            first_key = next(iter(entries.keys()))
            return f"{prefix}:{first_key}"
    return None


def _normalize_new_doc_target(raw_path_or_target: str, markdown_root: str) -> str:
    raw = str(raw_path_or_target or "").strip()
    if raw.startswith("document:"):
        raw = raw[len("document:") :].strip()
    if (
        raw
        and not raw.startswith(f"{markdown_root}/")
        and "/" in raw
        and not raw.startswith("/")
        and ".." not in raw.split("/")
        and len([p for p in raw.split("/") if p]) >= 2
        and raw.lower().endswith(".md")
    ):
        raw = f"{markdown_root}/{raw}"
    return validate_markdown_relpath(
        raw, markdown_root=markdown_root, require_category_subdir=True
    )


def _build_single_candidate(
    item: Mapping[str, Any],
    *,
    project_id: str,
    proj_type: str,
    md_root: str,
    src_dir: Path | None,
    values: Mapping[str, Any],
    generation: str,
    explicit_target: str | None,
    explicit_node: dict[str, Any] | None,
    explicit_doc: dict[str, Any] | None,
    explicit_generated: bool,
    all_docs: Sequence[Mapping[str, Any]],
    existing_overrides: Mapping[str, Any],
    default_reference_sql: str | None,
    source_type: str,
    raw_evidence: Mapping[str, Any],
    description_hint: str,
) -> SynthesizedProposal:
    route = str(item.get("route") or "").strip().lower()
    raw_target = str(item.get("target") or explicit_target or "").strip()
    reason = (
        str(item.get("reason") or "").strip()
        or scrub_untrusted_text(description_hint)[:200]
    )
    raw_structured = item.get("structured_patch")
    raw_patch = item.get("patch") if isinstance(item.get("patch"), str) else ""
    new_doc_path = str(item.get("new_doc_path") or "").strip()
    reference_sql = (
        str(item.get("reference_sql")).strip()
        if isinstance(item.get("reference_sql"), str)
        and item.get("reference_sql").strip()
        else default_reference_sql
    )

    if explicit_target and explicit_generated:
        route = "override"
        raw_target = explicit_target
    elif (
        explicit_target
        and explicit_target.startswith("document:")
        and not explicit_generated
    ):
        route = (
            "existing_document"
            if explicit_doc is not None
            else (
                "new_document"
                if route == "new_document" or new_doc_path
                else "existing_document"
            )
        )
        raw_target = explicit_target
    elif explicit_target and explicit_target.startswith(
        ("table:", "field:", "relationship:", "metric:")
    ):
        route = "override"
        raw_target = explicit_target

    if not route:
        if isinstance(raw_structured, Mapping) and raw_structured:
            route = "override"
        elif new_doc_path:
            route = "new_document"
        elif raw_patch.strip():
            matched = _match_existing_doc(raw_target, all_docs)
            route = "existing_document" if matched else "new_document"
        else:
            raise LLMUnavailableError(
                "大模型未返回有效的知识变更方案，请重试或切换为手工编辑"
            )

    if proj_type == "document" and route == "override":
        if raw_patch.strip():
            route = "new_document" if new_doc_path else "existing_document"
        else:
            raise LLMUnavailableError(
                "文档型项目不支持语义覆盖补丁，请切换为手工编辑文档"
            )

    target_kind: TargetKind
    resolved_path: str
    is_new_file = False
    original = ""
    structured_patch: dict[str, Any] | None = None
    patch_text = ""
    diff = ""
    schema_error: str | None = None

    if route == "override":
        if not isinstance(raw_structured, Mapping) or not raw_structured:
            raise LLMUnavailableError(
                "大模型未生成有效的结构化语义覆盖补丁，请重试或切换为手工编辑"
            )
        target_kind = "override"
        resolved_path = OVERRIDE_RELATIVE_PATH
        try:
            structured_patch = normalize_structured_patch(dict(raw_structured))
        except ValueError as exc:
            schema_error = str(exc)
            structured_patch = dict(raw_structured)
        if not raw_target or (
            raw_target.startswith("document:") and not explicit_generated
        ):
            derived = _derive_override_target(structured_patch)
            if derived:
                raw_target = derived
        if not raw_target:
            raise LLMUnavailableError(
                "大模型未能定位有效的语义覆盖目标，请指定目标或切换为手工编辑"
            )

        if src_dir and src_dir.is_dir():
            yaml_path = override_file_path(src_dir)
            if yaml_path.is_file():
                original = yaml_path.read_text(encoding="utf-8")
        if schema_error is None and structured_patch is not None:
            merged_yaml = dump_overrides_yaml(
                merge_overrides_doc(dict(existing_overrides), structured_patch)
            )
            patch_text = (
                raw_patch
                if raw_patch.strip()
                else dump_overrides_yaml(structured_patch)
            )
            diff = unified_diff_text(OVERRIDE_RELATIVE_PATH, original, merged_yaml)
        else:
            patch_text = raw_patch or json.dumps(
                raw_structured, ensure_ascii=False, indent=2
            )
            diff = unified_diff_text(OVERRIDE_RELATIVE_PATH, original, patch_text)
    else:
        if not raw_patch.strip():
            raise LLMUnavailableError(
                "大模型未生成有效的 Markdown 文档内容，请重试或切换为手工编辑"
            )
        target_kind = "document"
        matched_doc = explicit_doc or _match_existing_doc(raw_target, all_docs)
        if matched_doc is None and raw_target.startswith("document:"):
            fetched = fetch_node(values, generation, raw_target)
            if (
                fetched is not None
                and fetched.get("source_file")
                and not fetched.get("generated")
                and not str(fetched.get("source_file") or "").startswith("ossie/")
            ):
                matched_doc = {
                    "ref": raw_target,
                    "category": str(fetched.get("category") or ""),
                    "path": str(fetched.get("path") or ""),
                    "source_file": str(fetched.get("source_file") or ""),
                    "title": str(fetched.get("title") or ""),
                    "content": str(fetched.get("content") or ""),
                }
        if route == "existing_document" and matched_doc is not None:
            raw_target = str(matched_doc["ref"])
            resolved_path = str(matched_doc.get("source_file") or raw_target)
            original = str(matched_doc.get("content") or "")
            if src_dir and matched_doc.get("source_file"):
                cand_file = src_dir / str(matched_doc["source_file"])
                if cand_file.is_file():
                    original = cand_file.read_text(encoding="utf-8")
            is_new_file = False
        else:
            path_candidate = new_doc_path or raw_target
            try:
                rel_doc = _normalize_new_doc_target(path_candidate, md_root)
            except ValueError as exc:
                rel_doc = ""
                schema_error = str(exc)
                resolved_path = path_candidate.removeprefix("document:")
                raw_target = (
                    path_candidate
                    if path_candidate.startswith("document:")
                    else f"document:{resolved_path}"
                )
                is_new_file = True
            else:
                resolved_path = rel_doc
                raw_target = f"document:{rel_doc}"
                if src_dir and (src_dir / rel_doc).is_file():
                    original = (src_dir / rel_doc).read_text(encoding="utf-8")
                    is_new_file = False
                else:
                    matched_by_path = _match_existing_doc(rel_doc, all_docs)
                    if matched_by_path is not None:
                        raw_target = str(matched_by_path["ref"])
                        original = str(matched_by_path.get("content") or "")
                        is_new_file = False
                    else:
                        original = ""
                        is_new_file = True
        patch_text = raw_patch
        diff = unified_diff_text(
            resolved_path, original, patch_text, is_new_file=is_new_file
        )

    gate_report = run_l1_gate(
        target=raw_target,
        patch=patch_text,
        reason=reason,
        structured_patch=structured_patch,
        target_kind=target_kind,
        project_id=project_id,
        resolved_path=resolved_path,
        is_new_file=is_new_file,
    )
    gate_dict = gate_report.to_dict()
    if schema_error and schema_error not in gate_dict.get("errors", []):
        gate_dict["passed"] = False
        gate_dict.setdefault("errors", []).append(schema_error)

    dup = check_duplicate_candidate(
        project_id,
        target=raw_target,
        target_kind=target_kind,
        resolved_path=resolved_path,
        patch=patch_text,
        structured_patch=structured_patch,
        reason=reason,
        original=original,
        is_new_file=is_new_file,
        existing_overrides=existing_overrides,
        existing_docs=all_docs,
    )
    gate_dict.setdefault("checks", []).append(
        {
            "name": "anti_duplication",
            "passed": not dup.is_duplicate,
            "details": [dup.reason] if dup.is_duplicate and dup.reason else [],
        }
    )

    evidence = {
        "source_type": source_type,
        "summary_reason": reason,
        "trigger_evidence": dict(raw_evidence),
    }
    return SynthesizedProposal(
        target=raw_target,
        target_kind=target_kind,
        resolved_path=resolved_path,
        is_new_file=is_new_file,
        patch=patch_text,
        structured_patch=structured_patch,
        diff=diff,
        reason=reason,
        gate_report=gate_dict,
        used_llm=True,
        source_generated=explicit_generated,
        caliber=description_hint.strip() if target_kind == "document" else None,
        reference_sql=reference_sql,
        is_duplicate=dup.is_duplicate,
        duplicate_of=dup.duplicate_of,
        duplicate_reason=dup.reason,
        evidence=evidence,
    )


def _scrub_json_value(value: Any) -> Any:
    """Recursively scrub secrets and prompt-injection patterns across JSON-like values."""
    if isinstance(value, str):
        return scrub_untrusted_text(value)
    if isinstance(value, Mapping):
        return {str(k): _scrub_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub_untrusted_text(str(value))


def _check_intra_batch_duplicate(
    candidate: SynthesizedProposal, accepted: Sequence[SynthesizedProposal]
) -> DuplicateCheckResult:
    norm_patch = _normalize_text_for_dup(candidate.patch)
    norm_reason = _normalize_text_for_dup(candidate.reason)
    patch_fp = hashlib.sha256(norm_patch.encode("utf-8")).hexdigest()
    for prev in accepted:
        if (
            candidate.target_kind == "override"
            and prev.target_kind == "override"
            and candidate.structured_patch is not None
            and prev.structured_patch is not None
            and _overrides_overlap(candidate.structured_patch, prev.structured_patch)
        ):
            return DuplicateCheckResult(
                True,
                duplicate_of=f"batch_proposal:{prev.target}",
                reason=f"与同批次前序提案 {prev.target} 的结构化覆盖重叠",
            )
        if candidate.target_kind == "document" and prev.target_kind == "document":
            prev_norm_patch = _normalize_text_for_dup(prev.patch)
            prev_fp = hashlib.sha256(prev_norm_patch.encode("utf-8")).hexdigest()
            same_target = (
                candidate.target == prev.target
                or candidate.resolved_path == prev.resolved_path
            )
            if same_target and (
                prev_fp == patch_fp
                or (
                    norm_reason
                    and _jaccard_overlap(
                        norm_reason, _normalize_text_for_dup(prev.reason)
                    )
                    >= 0.8
                )
                or SequenceMatcher(None, norm_patch, prev_norm_patch).ratio() >= 0.9
            ):
                return DuplicateCheckResult(
                    True,
                    duplicate_of=f"batch_proposal:{prev.target}",
                    reason=f"与同批次前序文档提案 {prev.target} 重复",
                )
            if not same_target and (
                prev_fp == patch_fp
                or (
                    _jaccard_overlap(candidate.patch, prev.patch) >= 0.85
                    and _jaccard_overlap(candidate.reason, prev.reason) >= 0.8
                )
            ):
                return DuplicateCheckResult(
                    True,
                    duplicate_of=f"batch_proposal:{prev.target}",
                    reason=f"与同批次前序文档提案 {prev.target} 语义高度重复",
                )
    return DuplicateCheckResult(False)


def synthesize_proposals(
    *,
    project_id: str,
    source_type: ProposalSource,
    description: str,
    raw_evidence: Mapping[str, Any] | None = None,
    target: str | None = None,
    category: str | None = None,
    sql_blocks: Sequence[Mapping[str, Any]] = (),
    provider_id: str | None = None,
    model: str | None = None,
    allow_multi: bool = False,
    skip_duplicates: bool = False,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> list[SynthesizedProposal]:
    """Synthesize one or more validated, deduplicated knowledge proposals using the configured LLM."""
    provider = resolve_llm_provider(
        project_id,
        source_type=source_type,
        provider_id=provider_id,
        model=model,
    )
    if provider is None and llm_fn is None:
        raise LLMUnavailableError(
            "未配置可用的大模型提供方或模型，请先配置模型或切换为手工编辑"
        )

    clean_target = (
        target.strip() if isinstance(target, str) and target.strip() else None
    )
    ctx_bundle = collect_project_context(
        project_id,
        query_text=description,
        target=clean_target,
        sql_blocks=sql_blocks,
    )
    values = ctx_bundle["values"]
    generation = ctx_bundle["generation"]
    all_docs = ctx_bundle["all_documents"]
    src_dir: Path | None = ctx_bundle["source_dir"]

    explicit_node = (
        fetch_node(values, generation, clean_target) if clean_target else None
    )
    explicit_doc = _match_existing_doc(clean_target, all_docs) if clean_target else None
    is_doc_ref = bool(clean_target and clean_target.startswith("document:"))
    explicit_generated = bool(
        is_doc_ref
        and explicit_node is not None
        and (
            bool(explicit_node.get("generated"))
            or not explicit_node.get("source_file")
            or str(explicit_node.get("source_file") or "").startswith("ossie/")
        )
    )
    if explicit_doc is None and is_doc_ref and not explicit_generated and explicit_node:
        explicit_doc = {
            "ref": clean_target,
            "category": str(explicit_node.get("category") or ""),
            "path": str(explicit_node.get("path") or ""),
            "source_file": str(explicit_node.get("source_file") or ""),
            "title": str(explicit_node.get("title") or ""),
            "content": str(explicit_node.get("content") or ""),
        }

    if provider is None and llm_fn is not None:
        # Allow unit tests injecting a custom llm_fn stub without seeding Provider rows.
        pass

    scrubbed_evidence = _scrub_json_value(dict(raw_evidence or {}))
    llm_context: dict[str, Any] = {
        "project_id": project_id,
        "project_type": ctx_bundle["project_type"],
        "source_type": source_type,
        "markdown_root": ctx_bundle["markdown_root"],
        "allow_multi": allow_multi,
        "target": clean_target,
        "target_kind": (
            "override"
            if (clean_target and explicit_generated)
            or (
                clean_target
                and clean_target.startswith(
                    ("table:", "field:", "relationship:", "metric:")
                )
            )
            else ("document" if clean_target and is_doc_ref else None)
        ),
        "source_generated": explicit_generated,
        "category": category or "",
        "description": scrub_untrusted_text(description),
        "evidence": scrubbed_evidence,
        "sql": [
            scrub_untrusted_text(str(item.get("sql") or ""))
            for item in sql_blocks
            if isinstance(item, Mapping) and item.get("sql")
        ],
        "categories": ctx_bundle["categories"],
        "tables": ctx_bundle["tables"],
        "documents": ctx_bundle["documents"],
        "existing_overrides": ctx_bundle["existing_overrides"],
        "node_summary": (
            scrub_untrusted_text(str((explicit_node or {}).get("content") or "")[:2000])
            if explicit_node
            else ""
        ),
        "provider": provider,
    }

    caller = llm_fn or generate_with_llm
    if caller is generate_with_llm and provider is None:
        raise LLMUnavailableError(
            "未配置可用的大模型提供方或模型，请先配置模型或切换为手工编辑"
        )
    raw_output = caller(llm_context)
    if not isinstance(raw_output, Mapping):
        raise LLMUnavailableError(
            "大模型调用失败或未返回有效结构化结果，请重试或切换为手工编辑"
        )

    raw_items: list[Mapping[str, Any]]
    if isinstance(raw_output.get("proposals"), list):
        raw_items = [
            item for item in raw_output["proposals"] if isinstance(item, Mapping)
        ]
    else:
        raw_items = [raw_output]

    if not raw_items:
        raise LLMUnavailableError("大模型未返回可用的知识提案，请重试或切换为手工编辑")

    default_ref_sql: str | None = None
    if sql_blocks and isinstance(sql_blocks[-1], Mapping):
        default_ref_sql = str(sql_blocks[-1].get("sql") or "").strip() or None

    results: list[SynthesizedProposal] = []
    valid_count = 0
    batch_errors: list[Exception] = []
    for item in raw_items:
        try:
            candidate = _build_single_candidate(
                item,
                project_id=project_id,
                proj_type=ctx_bundle["project_type"],
                md_root=ctx_bundle["markdown_root"],
                src_dir=src_dir,
                values=values,
                generation=generation,
                explicit_target=clean_target,
                explicit_node=explicit_node,
                explicit_doc=explicit_doc,
                explicit_generated=explicit_generated,
                all_docs=all_docs,
                existing_overrides=ctx_bundle["existing_overrides"],
                default_reference_sql=default_ref_sql,
                source_type=source_type,
                raw_evidence=raw_evidence or {},
                description_hint=description,
            )
        except (LLMUnavailableError, ValueError, KeyError, TypeError) as exc:
            if not allow_multi:
                raise
            logger.warning("Skipping malformed batch synthesis item: %s", exc)
            batch_errors.append(exc)
            continue

        if not candidate.is_duplicate and results:
            batch_dup = _check_intra_batch_duplicate(candidate, results)
            if batch_dup.is_duplicate:
                updated_gate = dict(candidate.gate_report)
                updated_checks = [
                    c
                    for c in (updated_gate.get("checks") or [])
                    if not (
                        isinstance(c, Mapping) and c.get("name") == "anti_duplication"
                    )
                ]
                updated_checks.append(
                    {
                        "name": "anti_duplication",
                        "passed": False,
                        "details": [batch_dup.reason] if batch_dup.reason else [],
                    }
                )
                updated_gate["checks"] = updated_checks
                candidate = replace(
                    candidate,
                    gate_report=updated_gate,
                    is_duplicate=True,
                    duplicate_of=batch_dup.duplicate_of,
                    duplicate_reason=batch_dup.reason,
                )

        valid_count += 1
        if skip_duplicates and candidate.is_duplicate:
            continue
        results.append(candidate)
        if not allow_multi:
            break
    if valid_count == 0 and batch_errors:
        first_err = batch_errors[0]
        if isinstance(first_err, LLMUnavailableError):
            raise first_err
        raise LLMUnavailableError(str(first_err))
    return results


def synthesize_knowledge(
    *,
    project_id: str,
    source_type: ProposalSource = "manual",
    description: str,
    raw_evidence: Mapping[str, Any] | None = None,
    target: str | None = None,
    category: str | None = None,
    sql_blocks: Sequence[Mapping[str, Any]] = (),
    provider_id: str | None = None,
    model: str | None = None,
    llm_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> SynthesizedProposal:
    """Synthesize a single knowledge proposal for interactive drafting or single-topic induction."""
    proposals = synthesize_proposals(
        project_id=project_id,
        source_type=source_type,
        description=description,
        raw_evidence=raw_evidence,
        target=target,
        category=category,
        sql_blocks=sql_blocks,
        provider_id=provider_id,
        model=model,
        allow_multi=False,
        skip_duplicates=False,
        llm_fn=llm_fn,
    )
    if not proposals:
        raise LLMUnavailableError("大模型未返回可用的知识提案，请重试或切换为手工编辑")
    return proposals[0]
