"""Execute configured metadata pulls and record their outcome."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import project_context
from .crypto import decrypt
from .db import ProjectPullInstance, PullRecord, sessions
from .plugins import PullContext, PullItemFailure, discover
from .project_api import PULL_TOKEN_KEYS
from .project_api import PULLER_ADAPTER_IDS as PULLER_IDS
from .project_context import DEFAULT_PROJECT_ID, add_queued_build
from .pull_instances import decrypt_instance_secrets
from .source_directory import (
    git,
    publish_ossie_pull,
    publish_pull,
    publish_pull_instance,
    relative,
    repository,
    safe,
)

_locks: dict[str, threading.Lock] = {}
_instance_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


class PullConflictError(ValueError):
    """Raised when a pull is blocked by a concurrent pull or frozen project."""


class PullInstanceNotFoundError(LookupError):
    """Raised when the target ProjectPullInstance does not exist."""


def active_puller_id(project_id: str = DEFAULT_PROJECT_ID) -> str:
    """Return the single enabled puller id for a project, defaulting to 'dtb'."""
    enabled = [
        pid
        for pid in PULLER_IDS
        if (cfg := project_context.plugin_config("puller", pid, project_id)) and cfg[0]
    ]
    if len(enabled) > 1:
        raise ValueError("Multiple pullers cannot be enabled on the same project")
    return enabled[0] if enabled else "dtb"


def interval():
    for pid in PULLER_IDS:
        config = project_context.plugin_config("puller", pid)
        if config and config[0]:
            return int(config[1].get("interval_seconds", 0))
    return 0


def intervals() -> dict[str, int]:
    """Enabled pull intervals across DTB and Ossie pullers, keyed by project."""
    merged = project_context.plugin_intervals("puller", "dtb")
    merged.update(project_context.plugin_intervals("puller", "ossie"))
    return merged


def _lock_for(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def _instance_lock_for(project_id: str, instance_id: str) -> threading.Lock:
    with _locks_guard:
        return _instance_locks.setdefault((project_id, instance_id), threading.Lock())


def _reset_after_fork() -> None:
    global _locks_guard
    _locks_guard = threading.Lock()
    _locks.clear()
    _instance_locks.clear()


os.register_at_fork(after_in_child=_reset_after_fork)


def _existing_project_is_frozen(project_id: str) -> bool:
    """Frozen projects reject pulls; unknown projects keep the old failure path."""
    if not project_context.project_is_frozen(project_id):
        return False
    try:
        project_context.project_status(project_id)
    except project_context.ProjectNotFound:
        return False
    return True


def _frozen_pull_error(project_id: str) -> str:
    try:
        status = project_context.project_status(project_id)
    except project_context.ProjectNotFound:
        status = None
    if status == project_context.DELETING_STATUS:
        return "项目正在删除，拉取未继续"
    return f"{project_context.ARCHIVED_DETAIL}，拉取未继续"


def run_pull(project_id: str = DEFAULT_PROJECT_ID, *, puller_id: str | None = None):
    lock = _lock_for(project_id)
    if not lock.acquire(blocking=False):
        raise ValueError("A pull is already running")
    try:
        if _existing_project_is_frozen(project_id):
            raise ValueError(project_context.ARCHIVED_DETAIL)
        with sessions().begin() as db:
            record = PullRecord(
                project_id=project_id,
                status="running",
                started_at=datetime.now(UTC),
                tables=0,
                tasks=0,
                operation_id=uuid.uuid4().hex,
            )
            db.add(record)
            db.flush()
            record_id = record.id
            operation_id = record.operation_id
        try:
            selected_puller = puller_id or active_puller_id(project_id)
            if selected_puller not in PULLER_IDS:
                raise ValueError(f"Unsupported puller: {selected_puller}")
            config = project_context.plugin_config(
                "puller", selected_puller, project_id
            )
            secret = project_context.plugin_secret(
                project_id, PULL_TOKEN_KEYS[selected_puller]
            )
            directory = project_context.source_directory_value(project_id)
            if not config or not config[0] or not secret or not directory:
                label = "DTB" if selected_puller == "dtb" else "DTB Ossie"
                raise ValueError(f"{label} puller configuration is incomplete")
            parameters = config[1]
            token = decrypt(secret)
            source = directory
            url = parameters.get("url", "")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Invalid DTB URL")
            puller = discover()[("puller", selected_puller)]
            ossie_path = "ossie"
            if selected_puller == "ossie":
                ossie_cfg = project_context.plugin_config("source", "ossie", project_id)
                if ossie_cfg and isinstance(ossie_cfg[1].get("path"), str):
                    ossie_path = ossie_cfg[1]["path"] or "ossie"
                scope_keys = (
                    "layer",
                    "max_tables",
                    "model_name",
                    "low_cardinality_limit",
                    "sample_limit",
                )
            else:
                scope_keys = ("layer", "max_tables")
            scope = {k: parameters[k] for k in scope_keys if k in parameters}
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                if selected_puller == "ossie" and ossie_path != "ossie":
                    files, counts = puller.fetch(
                        client, url, token, scope or None, ossie_path=ossie_path
                    )
                else:
                    files, counts = (
                        puller.fetch(client, url, token, scope)
                        if scope
                        else puller.fetch(client, url, token)
                    )
            with sessions().begin() as db:
                record = db.get(PullRecord, record_id)
                record.source_directory = source
                record.tables, record.tasks = counts["tables"], counts["tasks"]
            with repository(source) as root:
                commit = (
                    publish_ossie_pull(
                        root, files, ossie_path=ossie_path, operation_id=operation_id
                    )
                    if selected_puller == "ossie"
                    else publish_pull(root, files, operation_id=operation_id)
                )
            finalize_pull(
                record_id,
                commit,
                counts,
                project_id=project_id,
                puller_id=selected_puller,
            )
        except Exception as exc:  # noqa: BLE001 - persist failures from plugins and storage
            with sessions().begin() as db:
                row = db.get(PullRecord, record_id)
                row.error = str(exc) if isinstance(exc, ValueError) else "Pull failed"
                row.finished_at = datetime.now(UTC)
            recover_pulls(record_id)
        with sessions()() as db:
            row = db.get(PullRecord, record_id)
            db.expunge(row)
            return row
    finally:
        lock.release()


def finalize_pull(
    record_id,
    commit,
    counts=None,
    project_id: str | None = None,
    *,
    puller_id: str | None = None,
):
    from .build_queue import has_successful_full_build

    with sessions().begin() as db:
        row = db.get(PullRecord, record_id)
        target = project_id or row.project_id or DEFAULT_PROJECT_ID
        if puller_id is not None:
            resolved_puller = puller_id
        else:
            try:
                resolved_puller = active_puller_id(target)
            except ValueError:
                resolved_puller = "dtb"
        build_type = (
            ("incremental" if has_successful_full_build(db, target) else "full")
            if resolved_puller == "ossie"
            else "incremental"
        )
        if row.build_id is None:
            row.build_id = add_queued_build(
                db, build_type, "定时拉取", project_id=target
            )
        failures = (counts or {}).get("failures") or []
        row.status, row.commit = "succeeded", commit
        row.error = (
            "Partial DTB pull failures: "
            + "; ".join(
                f"{failure['table']} ({failure['reason']})" for failure in failures
            )
            if failures
            else None
        )
        if counts:
            row.tables, row.tasks = counts["tables"], counts["tasks"]
        row.finished_at = datetime.now(UTC)
    project_context.wake_build_queue()


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _load_prior_dimension_values(
    directory: str, instance_id: str
) -> list[dict[str, Any]]:
    root = Path(directory).expanduser().resolve()
    dim_path = safe(root, relative(f"pulls/{instance_id}/dimension_values.json"))
    if not dim_path.is_file():
        return []
    try:
        parsed = json.loads(dim_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _dim_target_matches(row: Mapping[str, Any], target: str) -> bool:
    db_name = str(row.get("database") or "").strip().lower()
    tbl_name = str(row.get("table") or "").strip().lower()
    if not tbl_name:
        return False
    cleaned = target.strip().lower()
    if not cleaned:
        return False
    parts = [part for part in cleaned.split(".") if part]
    if len(parts) >= 2:
        return (db_name, tbl_name) == (parts[-2], parts[-1])
    return tbl_name == cleaned


def _carry_forward_failed_dimensions(
    directory: str,
    instance_id: str,
    dimension_values: list[dict[str, Any]],
    failures: list[PullItemFailure],
) -> tuple[list[dict[str, Any]], list[str]]:
    merged = [dict(row) for row in (dimension_values or []) if isinstance(row, dict)]
    if not failures:
        return merged, []

    dim_failures = [
        item for item in failures if getattr(item, "kind", None) == "dimension_table"
    ]
    other_failures = [
        item for item in failures if getattr(item, "kind", None) != "dimension_table"
    ]
    warnings: list[str] = []

    if dim_failures:
        prior_rows = _load_prior_dimension_values(directory, instance_id)
        present_tables = {
            (
                str(row.get("database") or "").strip().lower(),
                str(row.get("table") or "").strip().lower(),
            )
            for row in merged
        }
        carried_tables: set[tuple[str, str]] = set()
        for failure in dim_failures:
            target = str(getattr(failure, "target", "") or "")
            reason = str(getattr(failure, "reason", "") or "dimension_fetch_failed")
            matched = [
                row
                for row in prior_rows
                if _dim_target_matches(row, target)
                and (
                    str(row.get("database") or "").strip().lower(),
                    str(row.get("table") or "").strip().lower(),
                )
                not in present_tables
            ]
            if matched:
                matched_sorted = sorted(
                    matched,
                    key=lambda r: (
                        str(r.get("database") or ""),
                        str(r.get("table") or ""),
                        str(r.get("row_id") or ""),
                        str(r.get("field") or ""),
                        str(r.get("value") or ""),
                    ),
                )
                for row in matched_sorted:
                    tkey = (
                        str(row.get("database") or "").strip().lower(),
                        str(row.get("table") or "").strip().lower(),
                    )
                    if tkey not in carried_tables:
                        merged.append(row)
                for row in matched_sorted:
                    carried_tables.add(
                        (
                            str(row.get("database") or "").strip().lower(),
                            str(row.get("table") or "").strip().lower(),
                        )
                    )
                warnings.append(
                    f"维度表 {target} 拉取失败（{reason}），已沿用上次快照的 {len(matched_sorted)} 条维度值"
                )
            else:
                warnings.append(
                    f"维度表 {target} 拉取失败（{reason}），无可用历史维度快照"
                )

    for failure in other_failures:
        warnings.append(f"{failure.kind} {failure.target} 拉取失败（{failure.reason}）")
    return merged, warnings


def _finalize_instance_pull(
    record_id: int,
    commit: str,
    *,
    project_id: str,
    instance_id: str,
    table_count: int | None = None,
    task_count: int | None = None,
    warnings: list[str] | None = None,
) -> None:
    from .build_queue import has_successful_full_build

    with sessions().begin() as db:
        row = db.get(PullRecord, record_id)
        if row is None:
            return
        build_type = (
            "incremental" if has_successful_full_build(db, project_id) else "full"
        )
        if row.build_id is None:
            row.build_id = add_queued_build(
                db, build_type, "定时拉取", project_id=project_id
            )
        row.status = "succeeded"
        row.skipped_reason = None
        row.commit = commit
        row.error = None
        if warnings is not None:
            row.warnings = json.dumps(warnings, ensure_ascii=False)
        if table_count is not None:
            row.tables = table_count
        if task_count is not None:
            row.tasks = task_count
        row.finished_at = datetime.now(UTC)

        inst = db.get(ProjectPullInstance, (project_id, instance_id))
        if inst is not None and (table_count is not None or row.tables > 0):
            inst.last_table_count = (
                table_count if table_count is not None else row.tables
            )
            inst.updated_at = datetime.now(UTC)
    project_context.wake_build_queue()


def run_instance_pull(
    project_id: str,
    instance_id: str,
    *,
    trigger_type: str = "manual",
    force: bool = False,
) -> PullRecord:
    """Execute one plugin pull for a ProjectPullInstance with safeguards."""
    lock = _instance_lock_for(project_id, instance_id)
    if not lock.acquire(blocking=False):
        raise PullConflictError("A pull is already running")
    try:
        if _existing_project_is_frozen(project_id):
            raise PullConflictError(project_context.ARCHIVED_DETAIL)

        with sessions().begin() as db:
            inst = db.get(ProjectPullInstance, (project_id, instance_id))
            if inst is None:
                raise PullInstanceNotFoundError(f"拉取器实例不存在：{instance_id}")
            plugin_id = inst.plugin_id
            parameters = _parse_json_dict(inst.parameters)
            secrets = decrypt_instance_secrets(inst.encrypted_secrets)
            catalog_prefix = inst.catalog_prefix
            task_namespace = inst.task_namespace
            drop_threshold = float(inst.drop_threshold)
            last_table_count = inst.last_table_count

            effective_trigger = "forced" if force else trigger_type
            record = PullRecord(
                project_id=project_id,
                instance_id=instance_id,
                plugin_id=plugin_id,
                trigger_type=effective_trigger,
                status="running",
                started_at=datetime.now(UTC),
                tables=0,
                tasks=0,
                warnings="[]",
                operation_id=uuid.uuid4().hex,
            )
            db.add(record)
            db.flush()
            record_id = record.id
            operation_id = record.operation_id

        warnings: list[str] = []
        try:
            plugin = discover().get(("puller", plugin_id))
            if plugin is None:
                raise ValueError(f"Puller plugin not installed: {plugin_id}")
            directory = project_context.source_directory_value(project_id)
            if not directory:
                raise ValueError("Source directory is not configured")

            context = PullContext(
                project_id=project_id,
                instance_id=instance_id,
                parameters=parameters,
                secrets=secrets,
                catalog_prefix=catalog_prefix,
                task_namespace=task_namespace,
            )
            artifact = plugin.pull(context)

            table_count = int(artifact.table_count)
            task_count = int(artifact.task_count)
            merged_dims, warnings = _carry_forward_failed_dimensions(
                directory,
                instance_id,
                list(artifact.dimension_values or []),
                list(artifact.failures or []),
            )

            with sessions().begin() as db:
                row = db.get(PullRecord, record_id)
                row.source_directory = directory
                row.tables = table_count
                row.tasks = task_count
                row.warnings = json.dumps(warnings, ensure_ascii=False)

            zero_tables = table_count == 0
            drop_exceeded = (
                last_table_count is not None
                and last_table_count > 0
                and (last_table_count - table_count) / last_table_count > drop_threshold
            )
            if not force and (zero_tables or drop_exceeded):
                if zero_tables:
                    msg = (
                        "拉取结果包含 0 张表，触发熔断保护，已中止落盘与构建；"
                        "如确需空快照请使用 force=true 强制拉取"
                    )
                else:
                    drop_pct = (last_table_count - table_count) / last_table_count * 100
                    threshold_pct = drop_threshold * 100
                    msg = (
                        f"表数量从 {last_table_count} 骤降至 {table_count}"
                        f"（下降 {drop_pct:.1f}%，超过阈值 {threshold_pct:.1f}%），"
                        "触发熔断保护，已中止落盘与构建；"
                        "如确属预期变更请使用 force=true 强制拉取"
                    )
                warnings.append(msg)
                with sessions().begin() as db:
                    row = db.get(PullRecord, record_id)
                    row.status = "blocked"
                    row.error = msg
                    row.warnings = json.dumps(warnings, ensure_ascii=False)
                    row.commit = None
                    row.build_id = None
                    row.finished_at = datetime.now(UTC)
                with sessions()() as db:
                    row = db.get(PullRecord, record_id)
                    db.expunge(row)
                    return row

            with repository(directory) as root:
                commit, changed = publish_pull_instance(
                    root,
                    instance_id,
                    artifact.ossie_models,
                    tasks=artifact.tasks,
                    dimension_values=merged_dims,
                    operation_id=operation_id,
                )

            if not changed:
                with sessions().begin() as db:
                    row = db.get(PullRecord, record_id)
                    row.status = "succeeded"
                    row.skipped_reason = "no_changes"
                    row.commit = None
                    row.build_id = None
                    row.error = None
                    row.warnings = json.dumps(warnings, ensure_ascii=False)
                    row.finished_at = datetime.now(UTC)
                    inst = db.get(ProjectPullInstance, (project_id, instance_id))
                    if inst is not None and inst.last_table_count is None:
                        inst.last_table_count = table_count
                        inst.updated_at = datetime.now(UTC)
            else:
                _finalize_instance_pull(
                    record_id,
                    commit,
                    project_id=project_id,
                    instance_id=instance_id,
                    table_count=table_count,
                    task_count=task_count,
                    warnings=warnings,
                )
        except Exception as exc:  # noqa: BLE001 - persist failures from plugins and storage
            with sessions().begin() as db:
                row = db.get(PullRecord, record_id)
                row.error = str(exc) if isinstance(exc, ValueError) else "Pull failed"
                if warnings:
                    row.warnings = json.dumps(warnings, ensure_ascii=False)
                row.finished_at = datetime.now(UTC)
            recover_pulls(record_id)
        with sessions()() as db:
            row = db.get(PullRecord, record_id)
            db.expunge(row)
            return row
    finally:
        lock.release()


def recover_pulls(record_id=None):
    with sessions()() as db:
        query = db.query(PullRecord).filter(PullRecord.status == "running")
        if record_id is not None:
            query = query.filter(PullRecord.id == record_id)
        pending = [
            (r.id, r.source_directory, r.operation_id, r.project_id, r.instance_id)
            for r in query.all()
        ]
    for ident, directory, operation, project_id, instance_id in pending:
        if project_context.project_is_frozen(project_id):
            with sessions().begin() as db:
                row = db.get(PullRecord, ident)
                if row is not None and row.status == "running":
                    row.status = "failed"
                    row.error = _frozen_pull_error(project_id)
                    row.finished_at = datetime.now(UTC)
            continue
        commit = None
        if directory and operation:
            with repository(directory) as root:
                try:
                    raw = git(
                        root,
                        "log",
                        "--format=%H%x00%B%x00",
                        "--fixed-strings",
                        "--grep",
                        f"Pull-Operation: {operation}",
                    ).decode()
                    parts = raw.split("\0")
                    if (
                        len(parts) >= 2
                        and f"Pull-Operation: {operation}" in parts[1].splitlines()
                    ):
                        commit = parts[0].strip()
                except ValueError:
                    pass
        if commit:
            if instance_id:
                _finalize_instance_pull(
                    ident,
                    commit,
                    project_id=project_id,
                    instance_id=instance_id,
                )
            else:
                finalize_pull(ident, commit, project_id=project_id)
        else:
            with sessions().begin() as db:
                row = db.get(PullRecord, ident)
                row.status = "failed"
                row.error = row.error or "Pull interrupted before commit"
                row.finished_at = datetime.now(UTC)
