"""Execute configured metadata pulls and record their outcome."""

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
from .source_directory import git, publish_pull, repository

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def interval():
    config = project_context.plugin_config("puller", "dtb")
    return int(config[1].get("interval_seconds", 0)) if config and config[0] else 0


def intervals() -> dict[str, int]:
    """Enabled DTB pull intervals, keyed by project."""
    return project_context.plugin_intervals("puller", "dtb")


def _lock_for(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def run_pull(project_id: str = DEFAULT_PROJECT_ID):
    lock = _lock_for(project_id)
    if not lock.acquire(blocking=False):
        raise ValueError("A pull is already running")
    try:
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
            with sessions()() as db:
                config = project_context.plugin_config("puller", "dtb", project_id)
                secret = project_context.plugin_secret(project_id, "puller.dtb.token")
                directory = project_context.source_directory_value(project_id)
                if not config or not config[0] or not secret or not directory:
                    raise ValueError("DTB puller configuration is incomplete")
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
            puller = discover()[("puller", "dtb")]
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                scope = {
                    k: parameters[k] for k in ("layer", "max_tables") if k in parameters
                }
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
                commit = publish_pull(root, files, operation_id=operation_id)
            finalize_pull(record_id, commit, counts, project_id=project_id)
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


def finalize_pull(record_id, commit, counts=None, project_id: str | None = None):
    with sessions().begin() as db:
        row = db.get(PullRecord, record_id)
        target = project_id or row.project_id or DEFAULT_PROJECT_ID
        if row.build_id is None:
            row.build_id = add_queued_build(
                db, "incremental", "定时拉取", project_id=target
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
