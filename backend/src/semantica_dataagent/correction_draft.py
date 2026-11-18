"""Natural-language assisted correction drafting with target routing and L1 precheck."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import knowledge, project_context
from .auth import Requester
from .crypto import decrypt
from .db import Conversation, Provider, Turn, sessions
from .eval_questions import extract_tables_and_fields
from .project_api import resolve_project_scope
from .semantic_overrides import (
    OVERRIDE_RELATIVE_PATH,
    dump_overrides_yaml,
    load_overrides_file,
    merge_overrides_doc,
    normalize_structured_patch,
    override_file_path,
    run_l1_gate,
    scrub_untrusted_text,
)
from .storage_errors import StorageUnavailable

TargetKind = Literal["document", "override", "ossie"]


class DraftCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(default=None, min_length=1, max_length=120)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=120)
    turn_id: int | None = None
    target: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    provider_id: str | None = None
    model: str | None = None

    @field_validator(
        "project",
        "conversation_id",
        "target",
        "category",
        "description",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class DraftCorrectionResponse(BaseModel):
    target: str
    target_kind: TargetKind
    patch: str
    structured_patch: dict[str, Any] | None = None
    diff: str
    gate_report: dict[str, Any]
    resolved_path: str
    reason: str
    source_generated: bool = False
    caliber: str | None = None
    reference_sql: str | None = None
    used_llm: bool = False


def generate_with_llm(context: dict[str, Any]) -> dict[str, Any] | None:
    """Pluggable LLM draft generator; calls a configured provider only when requested/enabled."""
    provider_cfg = context.get("provider")
    if not isinstance(provider_cfg, dict) or not provider_cfg.get("base_url"):
        return None
    base_url = str(provider_cfg["base_url"]).rstrip("/")
    provider_type = str(provider_cfg.get("type") or "openai-chat")
    model = str(provider_cfg.get("model") or "")
    api_key = str(provider_cfg.get("api_key") or "")
    if not model:
        return None

    system_prompt = (
        "你是企业语义知识库纠错助手。根据用户纠错描述与脱敏上下文，输出纯 JSON 对象，"
        "包含 reason (string)、可选 patch (Markdown 全文，仅针对可编辑 document)、"
        "可选 structured_patch (遵循 semantic_overrides v1 schema 的 JSON 对象)。禁止输出额外解释。"
    )
    user_payload = json.dumps(
        {k: v for k, v in context.items() if k != "provider"},
        ensure_ascii=False,
    )
    try:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if provider_type == "anthropic-messages":
            if api_key:
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            resp = httpx.post(
                f"{base_url}/v1/messages",
                headers=headers,
                json={
                    "model": model,
                    "max_tokens": 1200,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_payload}],
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                block.get("text", "")
                for block in (data.get("content") or [])
                if isinstance(block, dict)
            )
        else:
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_payload},
                    ],
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get(
                "content"
            ) or ""
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 2:
                stripped = "\n".join(
                    lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
                ).strip()
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None


def _unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )


def _fetch_node(
    values: dict[str, Any], generation: str, ref: str
) -> dict[str, Any] | None:
    try:
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


def _infer_target_from_context(
    citations: list[dict[str, Any]],
    sql_blocks: list[dict[str, Any]],
    category: str | None,
) -> str | None:
    refs = [
        str(item.get("ref") or "").strip()
        for item in citations
        if isinstance(item, dict) and str(item.get("ref") or "").strip()
    ]
    cat = (category or "").lower()
    if cat in {"caliber", "document", "docs"}:
        for prefix in ("document:", "table:", "field:", "relationship:", "metric:"):
            match = next((r for r in refs if r.startswith(prefix)), None)
            if match:
                return match
    elif cat in {"relationship", "join"}:
        for prefix in ("relationship:", "table:", "field:", "document:"):
            match = next((r for r in refs if r.startswith(prefix)), None)
            if match:
                return match
    else:
        for prefix in ("field:", "table:", "relationship:", "metric:", "document:"):
            match = next((r for r in refs if r.startswith(prefix)), None)
            if match:
                return match

    for block in reversed(sql_blocks):
        if isinstance(block, dict) and str(block.get("sql") or "").strip():
            dialect = str(block.get("dialect") or "hive")
            try:
                tables, fields = extract_tables_and_fields(str(block["sql"]), dialect)
                if fields and not fields[0].endswith(".*"):
                    return f"field:{fields[0]}"
                if tables:
                    return f"table:{tables[0]}"
            except ValueError:
                continue
    return None


def _parse_table_parts(ref_body: str) -> tuple[str | None, str, str] | None:
    parts = [p.strip() for p in ref_body.split(".") if p.strip()]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return None


def _parse_field_parts(ref_body: str) -> tuple[str | None, str, str, str] | None:
    parts = [p.strip() for p in ref_body.split(".") if p.strip()]
    if len(parts) == 3:
        return None, parts[0], parts[1], parts[2]
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    return None


def _structured_from_target(
    target: str,
    description: str,
    node: dict[str, Any] | None,
    sql_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    if target.startswith("table:"):
        parsed = _parse_table_parts(target[len("table:") :])
        if parsed:
            catalog, database, name = parsed
            entry: dict[str, Any] = {
                "database": database,
                "name": name,
                "instructions": description,
            }
            if catalog:
                entry["catalog"] = catalog
            key = f"{catalog}.{database}.{name}" if catalog else f"{database}.{name}"
            return {"version": 1, "tables": {key: entry}}
    if target.startswith("field:"):
        parsed = _parse_field_parts(target[len("field:") :])
        if parsed:
            catalog, database, table, name = parsed
            entry = {
                "database": database,
                "table": table,
                "name": name,
                "instructions": description,
            }
            if catalog:
                entry["catalog"] = catalog
            key = (
                f"{catalog}.{database}.{table}.{name}"
                if catalog
                else f"{database}.{table}.{name}"
            )
            return {"version": 1, "fields": {key: entry}}
    if node and node.get("database") and node.get("table") and node.get("name"):
        database = str(node["database"])
        table = str(node["table"])
        name = str(node["name"])
        key = f"{database}.{table}.{name}"
        entry = {
            "database": database,
            "table": table,
            "name": name,
            "instructions": description,
        }
        if node.get("catalog"):
            entry["catalog"] = str(node["catalog"])
        return {"version": 1, "fields": {key: entry}}
    if node and node.get("database") and node.get("name"):
        database = str(node["database"])
        name = str(node["name"])
        key = f"{database}.{name}"
        entry = {
            "database": database,
            "name": name,
            "instructions": description,
        }
        if node.get("catalog"):
            entry["catalog"] = str(node["catalog"])
        return {"version": 1, "tables": {key: entry}}
    if target.startswith("relationship:") and node:
        from_db = str(node.get("from_database") or "").strip()
        from_tbl = str(node.get("from_table") or "").strip()
        to_db = str(node.get("to_database") or "").strip()
        to_tbl = str(node.get("to_table") or "").strip()
        from_cols = [str(c) for c in (node.get("from_columns") or []) if str(c).strip()]
        to_cols = [str(c) for c in (node.get("to_columns") or []) if str(c).strip()]
        if from_db and from_tbl and to_db and to_tbl and from_cols and to_cols:
            rel_name = str(node.get("name") or target.split(":", 1)[1]).strip()
            return {
                "version": 1,
                "relationships": {
                    rel_name: {
                        "name": rel_name,
                        "from_database": from_db,
                        "from_table": from_tbl,
                        "from_columns": from_cols,
                        "to_database": to_db,
                        "to_table": to_tbl,
                        "to_columns": to_cols,
                        "instructions": description,
                    }
                },
            }
    # Fallback to first SQL table when drafting for a derived doc or metric without explicit schema node
    for block in reversed(sql_blocks):
        if isinstance(block, dict) and str(block.get("sql") or "").strip():
            try:
                tables, _ = extract_tables_and_fields(
                    str(block["sql"]), str(block.get("dialect") or "hive")
                )
            except ValueError:
                continue
            if tables:
                parsed = _parse_table_parts(tables[0])
                if parsed:
                    catalog, database, name = parsed
                    key = f"{database}.{name}"
                    entry = {
                        "database": database,
                        "name": name,
                        "instructions": description,
                    }
                    if catalog:
                        entry["catalog"] = catalog
                    return {"version": 1, "tables": {key: entry}}
    raise HTTPException(
        422,
        detail="未能解析出可校验的结构化语义覆盖目标，请指定具体的表、字段或可编辑文档引用",
    )


def _load_requested_provider(
    provider_id: str | None, model: str | None
) -> dict[str, Any] | None:
    if not provider_id:
        return None
    with sessions()() as db:
        row = db.get(Provider, provider_id)
        if row is None or not row.enabled:
            return None
        chosen_model = model
        if not chosen_model:
            try:
                models = json.loads(row.models or "[]")
                chosen_model = str(models[0]) if models else ""
            except ValueError:
                chosen_model = ""
        api_key = ""
        if row.api_key:
            try:
                api_key = decrypt(row.api_key)
            except (InvalidToken, ValueError):
                api_key = ""
        return {
            "id": row.id,
            "type": row.type,
            "base_url": row.base_url,
            "model": chosen_model,
            "api_key": api_key,
        }


def draft_correction_in_project(
    body: DraftCorrectionRequest,
    requester: Requester,
    *,
    query_project: str | None = None,
) -> DraftCorrectionResponse:
    """Build a natural-language assisted correction preview with L1 static precheck."""
    scope = resolve_project_scope(
        body.project or query_project, requester, allow_frozen=False
    )
    values = knowledge.settings(scope.project_id)
    project = project_context.from_values(values, scope.project_id)

    question = ""
    answer = ""
    sql_blocks: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    if body.conversation_id:
        with sessions()() as db:
            conv = db.get(Conversation, body.conversation_id)
            if (
                conv is None
                or conv.owner_id != requester.id
                or conv.project_id != scope.project_id
            ):
                raise HTTPException(404, detail="Conversation not found")
            if body.turn_id is not None:
                turn = db.get(Turn, body.turn_id)
                if turn is None or turn.conversation_id != body.conversation_id:
                    raise HTTPException(404, detail="Turn not found")
            else:
                turn = (
                    db.query(Turn)
                    .filter(Turn.conversation_id == body.conversation_id)
                    .order_by(Turn.sequence.desc())
                    .first()
                )
                if turn is None:
                    raise HTTPException(404, detail="Turn not found")
            question = turn.question
            answer = turn.answer
            sql_blocks = json.loads(turn.sql or "[]")
            citations = json.loads(turn.citations or "[]")

    target = body.target or _infer_target_from_context(
        citations, sql_blocks, body.category
    )
    if not target:
        raise HTTPException(
            422, detail="请指定纠错目标或选择包含表、字段或文档引用的对话轮次"
        )

    node = _fetch_node(values, project.generation, target)
    is_document_ref = target.startswith("document:")
    source_generated = bool(
        is_document_ref
        and node is not None
        and (
            bool(node.get("generated"))
            or not node.get("source_file")
            or str(node.get("source_file") or "").startswith("ossie/")
        )
    )

    if is_document_ref and not source_generated:
        target_kind: TargetKind = "document"
        resolved_path = (
            str(node.get("source_file")) if node and node.get("source_file") else target
        )
        original = str(node.get("content") or "") if node else ""
        if project.source_directory_value and node and node.get("source_file"):
            candidate_file = Path(project.source_directory_value) / str(
                node["source_file"]
            )
            if candidate_file.is_file():
                original = candidate_file.read_text(encoding="utf-8")
    else:
        target_kind = "override"
        resolved_path = OVERRIDE_RELATIVE_PATH
        original = ""
        if project.source_directory_value:
            yaml_path = override_file_path(Path(project.source_directory_value))
            if yaml_path.is_file():
                original = yaml_path.read_text(encoding="utf-8")

    # Always scrub untrusted text before handing context to an LLM.
    llm_context: dict[str, Any] = {
        "project_id": scope.project_id,
        "target": target,
        "target_kind": target_kind,
        "source_generated": source_generated,
        "category": body.category or "",
        "description": scrub_untrusted_text(body.description),
        "question": scrub_untrusted_text(question),
        "answer": scrub_untrusted_text(answer),
        "sql": [
            scrub_untrusted_text(str(item.get("sql") or ""))
            for item in sql_blocks
            if isinstance(item, dict) and item.get("sql")
        ],
        "node_summary": scrub_untrusted_text(original[:2000]),
        "provider": _load_requested_provider(body.provider_id, body.model),
    }

    llm_output = generate_with_llm(llm_context)
    used_llm = llm_output is not None

    reason = (
        str((llm_output or {}).get("reason") or "").strip()
        or scrub_untrusted_text(body.description)[:200]
        or body.description[:200]
    )

    structured_patch: dict[str, Any] | None = None
    patch = ""
    reference_sql: str | None = None
    if llm_output and isinstance(llm_output.get("reference_sql"), str):
        reference_sql = llm_output["reference_sql"].strip() or None
    elif sql_blocks and isinstance(sql_blocks[-1], dict):
        reference_sql = str(sql_blocks[-1].get("sql") or "").strip() or None

    schema_error: str | None = None
    if target_kind == "document":
        if (
            llm_output
            and isinstance(llm_output.get("patch"), str)
            and llm_output["patch"].strip()
        ):
            patch = llm_output["patch"]
        else:
            base = original.rstrip("\n")
            addition = f"## 纠错补充说明\n\n{body.description.strip()}\n"
            patch = f"{base}\n\n{addition}" if base else addition
        diff = _unified_diff(resolved_path, original, patch)
    else:
        raw_structured = (
            llm_output.get("structured_patch")
            if llm_output and isinstance(llm_output.get("structured_patch"), dict)
            else _structured_from_target(
                target, body.description.strip(), node, sql_blocks
            )
        )
        try:
            structured_patch = normalize_structured_patch(raw_structured)
        except ValueError as exc:
            schema_error = str(exc)
            structured_patch = (
                raw_structured if isinstance(raw_structured, dict) else None
            )
        src_root = (
            Path(project.source_directory_value)
            if project.source_directory_value
            else None
        )
        base_doc, _ = (
            load_overrides_file(src_root)
            if src_root and src_root.is_dir()
            else ({"version": 1}, None)
        )
        if schema_error is None and structured_patch is not None:
            merged_yaml = dump_overrides_yaml(
                merge_overrides_doc(base_doc, structured_patch)
            )
            patch = (
                str(llm_output.get("patch"))
                if llm_output
                and isinstance(llm_output.get("patch"), str)
                and llm_output["patch"].strip()
                else dump_overrides_yaml(structured_patch)
            )
            diff = _unified_diff(OVERRIDE_RELATIVE_PATH, original, merged_yaml)
        else:
            patch = (
                str(llm_output.get("patch"))
                if llm_output
                and isinstance(llm_output.get("patch"), str)
                and llm_output["patch"].strip()
                else dump_overrides_yaml(raw_structured)
                if isinstance(raw_structured, dict)
                else ""
            )
            diff = _unified_diff(OVERRIDE_RELATIVE_PATH, original, patch)

    gate_report = run_l1_gate(
        target=target,
        patch=patch,
        reason=reason,
        structured_patch=structured_patch,
        target_kind=target_kind,
        project_id=scope.project_id,
    )
    gate_dict = gate_report.to_dict()
    if schema_error and schema_error not in gate_dict.get("errors", []):
        gate_dict["passed"] = False
        gate_dict.setdefault("errors", []).append(schema_error)

    return DraftCorrectionResponse(
        target=target,
        target_kind=target_kind,
        patch=patch,
        structured_patch=structured_patch,
        diff=diff,
        gate_report=gate_dict,
        resolved_path=resolved_path,
        reason=reason,
        source_generated=source_generated,
        caliber=body.description.strip() if target_kind == "document" else None,
        reference_sql=reference_sql,
        used_llm=used_llm,
    )
