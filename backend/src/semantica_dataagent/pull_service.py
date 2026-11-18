"""Execute configured metadata pulls and record their outcome."""

import os
import threading
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from . import project_context
from .crypto import decrypt
from .db import PullRecord, sessions
from .plugins import discover
from .project_context import DEFAULT_PROJECT_ID, add_queued_build
from .source_directory import git, publish_ossie_pull, publish_pull, repository

PULLER_IDS = ("dtb", "ossie")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


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


def _reset_after_fork() -> None:
    global _locks_guard
    _locks_guard = threading.Lock()
    _locks.clear()


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
                project_id, f"puller.{selected_puller}.token"
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


def recover_pulls(record_id=None):
    with sessions()() as db:
        query = db.query(PullRecord).filter(PullRecord.status == "running")
        if record_id is not None:
            query = query.filter(PullRecord.id == record_id)
        pending = [
            (r.id, r.source_directory, r.operation_id, r.project_id)
            for r in query.all()
        ]
    for ident, directory, operation, project_id in pending:
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
            finalize_pull(ident, commit, project_id=project_id)
        else:
            with sessions().begin() as db:
                row = db.get(PullRecord, ident)
                row.status = "failed"
                row.error = row.error or "Pull interrupted before commit"
                row.finished_at = datetime.now(UTC)
