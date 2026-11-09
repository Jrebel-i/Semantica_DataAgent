#!/usr/bin/env python3
"""Build data/dtb-sample against isolated test storage and clean its generation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
import uuid
import warnings
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from neo4j import GraphDatabase
from pymilvus import MilvusClient

from semantica_dataagent import knowledge
from semantica_dataagent.app import SECRET
from semantica_dataagent.cli import initialize
from semantica_dataagent.db import Build, PluginConfig, Setting, sessions

DEFAULT_SOURCE = Path("data/dtb-sample")
DEFAULT_REPORT = Path("data/sample-build.json")
DEFAULT_DB = Path("data/.sample-build/app.db")
NEO4J_PASSWORD_KEY = next(iter(SECRET))
DB_APPLICATION_ID = 0x53444131


def _setting(key, value, cipher):
    stored = (
        cipher.encrypt(value.encode()).decode() if key == NEO4J_PASSWORD_KEY else value
    )
    return Setting(key=key, value=stored)


def _is_own_database(path):
    path = Path(path)
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            return (
                db.execute("PRAGMA application_id").fetchone()[0] == DB_APPLICATION_ID
            )
    except (OSError, sqlite3.Error):
        return False


def _mark_database(path):
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA application_id = {DB_APPLICATION_ID}")
        db.commit()


def _remove_own_database(path):
    path = Path(path)
    if not path.exists():
        return "absent"
    if not _is_own_database(path):
        return "not_owned"
    path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return "deleted"


def prepare_database(
    source, generation, neo4j, milvus, embedding, neo4j_secret, db_path
):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        if not _is_own_database(db_path):
            raise ValueError(f"refusing to delete database without marker: {db_path}")
        db_path.unlink()
    key = Fernet.generate_key().decode()
    os.environ["DATAAGENT_DB_PATH"] = str(db_path)
    os.environ["DATAAGENT_ENCRYPTION_KEY"] = key
    initialize()
    _mark_database(db_path)
    cipher = Fernet(key.encode())
    values = {
        "neo4j_uri": neo4j["uri"],
        "neo4j_user": neo4j["user"],
        NEO4J_PASSWORD_KEY: neo4j_secret,
        "milvus_uri": milvus,
        "embedding_url": embedding,
        "embedding_model": "model/bge-m3",
        "source_directory": str(Path(source).resolve()),
        "knowledge_generation": generation,
        "dimension_value_limit": "1000",
        "embedding_concurrency": "2",
        "embedding_timeout_seconds": "60",
        "embedding_timeout_per_input_seconds": "20",
        "embedding_retries": "2",
        "embedding_backoff_seconds": "1",
    }
    with sessions().begin() as db:
        for key_name, value in values.items():
            db.add(_setting(key_name, value, cipher))
        db.add(
            PluginConfig(
                category="source", plugin_id="canonical", enabled=False, parameters="{}"
            )
        )
        db.add(
            PluginConfig(
                category="source",
                plugin_id="dtb",
                enabled=True,
                parameters=json.dumps(
                    {
                        "snapshot_path": "dtb",
                        "dimensions_path": "dimensions",
                        "latest": True,
                        "dimension_row_limit": 50000,
                    }
                ),
            )
        )
        db.add(
            PluginConfig(
                category="source",
                plugin_id="markdown",
                enabled=True,
                parameters=json.dumps({"path": "knowledge"}),
            )
        )
        db.add(
            PluginConfig(
                category="parser", plugin_id="sql", enabled=True, parameters="{}"
            )
        )
        db.add(
            PluginConfig(
                category="parser", plugin_id="dtb", enabled=True, parameters="{}"
            )
        )
        row = Build(
            status="running", started_at=datetime.now(UTC).isoformat(), counts="{}"
        )
        db.add(row)
        db.flush()
        return row.id


def _graph_counts(neo4j, generation):
    with GraphDatabase.driver(
        neo4j["uri"], auth=(neo4j["user"], neo4j["neo4j_secret"])
    ) as driver:
        rows, _, _ = driver.execute_query(
            "MATCH (n:SDAEntity {generation:$generation}) "
            "RETURN labels(n) AS labels, count(*) AS count ORDER BY count DESC",
            generation=generation,
        )
    return [{"labels": row["labels"], "count": row["count"]} for row in rows]


def cleanup(neo4j, milvus, generation):
    result = {"neo4j": "pending", "milvus": "pending"}
    try:
        with GraphDatabase.driver(
            neo4j["uri"], auth=(neo4j["user"], neo4j["neo4j_secret"])
        ) as driver:
            driver.execute_query(
                "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
                generation=generation,
            )
        result["neo4j"] = "deleted"
    except Exception as exc:  # noqa: BLE001 - cleanup must continue to Milvus
        result["neo4j"] = f"failed: {type(exc).__name__}: {exc}"
    client = None
    try:
        client = MilvusClient(uri=milvus)
        name = "sda_" + generation
        if client.has_collection(name):
            client.drop_collection(name)
            result["milvus"] = "dropped"
        else:
            result["milvus"] = "absent"
    except Exception as exc:  # noqa: BLE001 - report cleanup failure
        result["milvus"] = f"failed: {type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            client.close()
    return result


def expected_counts(source):
    manifest = json.loads(
        (Path(source) / "dtb/manifest.json").read_text(encoding="utf-8")
    )
    counts = manifest["counts"]
    generated = sum(
        1
        for category in (Path(source) / "knowledge").iterdir()
        if category.is_dir() and not (category / "index.md").is_file()
    )
    return {
        "table": counts["tables"],
        "field": counts["fields"],
        "task": counts["tasks"],
        "chunk": counts["chunks"],
        "document": counts["documents"] + generated,
        "filtered": 0,
    }


def run(
    source, generation, neo4j, milvus, embedding, neo4j_secret, db_path, report_path
):
    start = time.monotonic()
    build_id = prepare_database(
        source, generation, neo4j, milvus, embedding, neo4j_secret, db_path
    )
    calls = {"requests": 0, "texts": 0}
    original_embed = knowledge.embed

    def counted_embed(values, texts, stats=None):
        calls["requests"] += 1
        calls["texts"] += len(texts)
        return original_embed(values, texts, stats)

    knowledge.embed = counted_embed
    try:
        knowledge.run_build(build_id)
        with sessions()() as db:
            row = db.get(Build, build_id)
            report = {
                "generation": generation,
                "status": row.status,
                "error": row.error,
                "counts": json.loads(row.counts),
                "parse_failures": json.loads(row.parse_failures or "[]"),
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "embedding_calls": calls,
                "elapsed_seconds": round(time.monotonic() - start, 1),
            }
        expected = expected_counts(source)
        report["expected"] = expected
        report["count_checks"] = {
            key: report["counts"].get(key) == value for key, value in expected.items()
        }
        report["graph_counts"] = _graph_counts(neo4j, generation)
        client = MilvusClient(uri=milvus)
        try:
            report["milvus_rows"] = client.get_collection_stats("sda_" + generation)
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            report["milvus_rows"] = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            client.close()
    finally:
        knowledge.close_readers()
        knowledge.embed = original_embed
        bind = getattr(sessions(), "kw", {}).get("bind")
        if bind is not None:
            bind.dispose()
        report = locals().get("report", {"generation": generation})
        report["cleanup"] = cleanup(neo4j, milvus, generation)
        report["database_cleanup"] = _remove_own_database(db_path)
        report["elapsed_seconds"] = round(time.monotonic() - start, 1)
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--generation", default="issue10sample_" + uuid.uuid4().hex[:8])
    parser.add_argument("--neo4j-uri", default="bolt://172.16.242.124:31611")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--milvus-uri", default="http://172.16.242.124:31612")
    parser.add_argument("--embedding-url", default="http://172.16.242.124:31614")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", message=".*ORM-style PyMilvus.*")
    neo4j_secret = os.environ.get("NEO4J_PASSWORD")
    if not neo4j_secret:
        raise SystemExit("NEO4J_PASSWORD must be set in the environment")
    if not args.generation[0].isalpha():
        raise SystemExit("generation must start with a letter")
    report = run(
        args.source,
        args.generation,
        {"uri": args.neo4j_uri, "user": args.neo4j_user, "neo4j_secret": neo4j_secret},
        args.milvus_uri,
        args.embedding_url,
        neo4j_secret,
        args.db,
        args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    checks = report.get("count_checks", {})
    raise SystemExit(
        0 if report.get("status") == "succeeded" and all(checks.values()) else 1
    )


if __name__ == "__main__":
    main()
