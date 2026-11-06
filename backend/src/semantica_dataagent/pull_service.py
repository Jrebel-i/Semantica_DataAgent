"""Execute configured metadata pulls and record their outcome."""

import json
import threading
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .build_queue import enqueue_build
from .crypto import decrypt
from .db import PluginConfig, PullRecord, Setting, sessions
from .plugins import discover
from .source_directory import publish_pull, repository

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
                status="running", started_at=datetime.now(UTC), tables=0, tasks=0
            )
            db.add(record)
            db.flush()
            record_id = record.id
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
            with repository(source) as root:
                commit = publish_pull(root, files)
            build_id = enqueue_build("incremental", "定时拉取")
            with sessions().begin() as db:
                row = db.get(PullRecord, record_id)
                row.status, row.commit, row.build_id = "succeeded", commit, build_id
                row.tables, row.tasks = counts["tables"], counts["tasks"]
                row.finished_at = datetime.now(UTC)
        except Exception as exc:  # noqa: BLE001 - persist failures from plugins and storage
            with sessions().begin() as db:
                row = db.get(PullRecord, record_id)
                row.status = "failed"
                row.error = str(exc) if isinstance(exc, ValueError) else "Pull failed"
                row.finished_at = datetime.now(UTC)
        with sessions()() as db:
            row = db.get(PullRecord, record_id)
            db.expunge(row)
            return row
    finally:
        _lock.release()
