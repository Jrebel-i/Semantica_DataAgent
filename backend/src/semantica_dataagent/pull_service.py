"""Execute configured metadata pulls and record their outcome."""

import json
import threading
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .build_queue import add_queued_build, build_queue
from .crypto import decrypt
from .db import PluginConfig, PullRecord, Setting, sessions
from .plugins import discover
from .source_directory import git, publish_pull, repository

_lock = threading.Lock()


def interval():
    with sessions()() as db:
        row = db.get(PluginConfig, ("puller", "dtb"))
        return (
            int(json.loads(row.parameters).get("interval_seconds", 0))
            if row and row.enabled
            else 0
        )


def run_pull():
    if not _lock.acquire(blocking=False):
        raise ValueError("A pull is already running")
    try:
        with sessions().begin() as db:
            record = PullRecord(
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
                config = db.get(PluginConfig, ("puller", "dtb"))
                secret = db.get(Setting, "puller.dtb.token")
                directory = db.get(Setting, "source_directory")
                if not config or not config.enabled or not secret or not directory:
                    raise ValueError("DTB puller configuration is incomplete")
                parameters = json.loads(config.parameters)
                token = decrypt(secret.value)
                source = directory.value
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
                files, counts = puller.fetch(client, url, token)
            with sessions().begin() as db:
                db.get(PullRecord, record_id).source_directory = source
            with repository(source) as root:
                commit = publish_pull(root, files, operation_id=operation_id)
            finalize_pull(record_id, commit, counts)
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
        _lock.release()


def finalize_pull(record_id, commit, counts=None):
    with sessions().begin() as db:
        row = db.get(PullRecord, record_id)
        if row.build_id is None:
            row.build_id = add_queued_build(db, "incremental", "定时拉取")
        row.status, row.commit, row.error = "succeeded", commit, None
        if counts:
            row.tables, row.tasks = counts["tables"], counts["tasks"]
        row.finished_at = datetime.now(UTC)
    build_queue.enqueue()


def recover_pulls(record_id=None):
    with sessions()() as db:
        query = db.query(PullRecord).filter(PullRecord.status == "running")
        if record_id is not None:
            query = query.filter(PullRecord.id == record_id)
        pending = [(r.id, r.source_directory, r.operation_id) for r in query.all()]
    for ident, directory, operation in pending:
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
            finalize_pull(ident, commit)
        else:
            with sessions().begin() as db:
                row = db.get(PullRecord, ident)
                row.status = "failed"
                row.error = row.error or "Pull interrupted before commit"
                row.finished_at = datetime.now(UTC)
