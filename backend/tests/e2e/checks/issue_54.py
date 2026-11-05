"""Restart recovery against the harness's isolated real stores."""

import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from neo4j import GraphDatabase
from pymilvus import MilvusClient


def check_interrupted_generation_recovery(ctx):
    runtime = ctx.runtime
    orphan = ctx.config.generation + "_orphan_" + uuid.uuid4().hex[:8]
    driver = GraphDatabase.driver(
        ctx.config.neo4j_uri, auth=(ctx.config.neo4j_user, runtime.password)
    )
    vectors = MilvusClient(uri=ctx.config.milvus_uri)
    try:
        driver.execute_query(
            "CREATE (:SDAEntity {generation:$generation})", generation=orphan
        )
        vectors.create_collection("sda_" + orphan, dimension=2)
        with sqlite3.connect(runtime.db_path) as db:
            cursor = db.execute(
                "INSERT INTO builds (status, started_at, counts, build_type, generation) VALUES ('running', '2026-01-01', '{}', 'full', ?)",
                (orphan,),
            )
            build_id = cursor.lastrowid
        process = runtime.server
        env = dict(
            item.split("=", 1)
            for item in Path(f"/proc/{process.pid}/environ")
            .read_bytes()
            .decode()
            .split("\0")
            if "=" in item
        )
        process.kill()
        process.wait(timeout=30)
        runtime.server = subprocess.Popen(
            process.args,
            cwd=os.getcwd(),
            env=env,
            stdout=runtime.service_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + ctx.config.start_timeout
        while time.monotonic() < deadline:
            try:
                response = ctx.rest.get(f"/api/v1/builds/{build_id}", timeout=2)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if runtime.server.poll() is not None:
                raise AssertionError("restarted service exited")
            time.sleep(0.2)
        else:
            raise AssertionError("restarted service did not become ready")
        build = response.json()
        ctx.require(
            build["status"] == "failed" and "interrupted" in build["error"].lower(),
            "interrupted build was not failed",
        )
        ctx.require(
            not vectors.has_collection("sda_" + orphan),
            "unpublished collection remains",
        )
        rows, _, _ = driver.execute_query(
            "MATCH (n:SDAEntity {generation:$generation}) RETURN count(n) AS n",
            generation=orphan,
        )
        ctx.require(rows[0]["n"] == 0, "unpublished nodes remain")
        generations = ctx.rest.get("/api/v1/generations").json()["generations"]
        ctx.require(
            any(
                row["name"] == ctx.generation and row["current"] for row in generations
            ),
            "current generation changed",
        )
        response = ctx.rest.get("/api/v1/tables", params={"database": "shop"})
        response.raise_for_status()
        ctx.require(
            response.json(), "current generation retrieval failed after restart"
        )
        return {"build_id": build_id, "current": ctx.generation}
    finally:
        driver.execute_query(
            "MATCH (n:SDAEntity {generation:$generation}) DETACH DELETE n",
            generation=orphan,
        )
        if vectors.has_collection("sda_" + orphan):
            vectors.drop_collection("sda_" + orphan)
        vectors.close()
        driver.close()
