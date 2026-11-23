"""Seam 1 + crash recovery tests for instance pull lifecycle and safeguards."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from semantica_dataagent import (
    plugins,
    project_context,
    pull_instances,
    pull_service,
    source_directory,
)
from semantica_dataagent.db import Build, ProjectPullInstance, PullRecord, sessions
from semantica_dataagent.plugins import PullArtifact, PullContext, PullItemFailure


class _MemoryStubPuller:
    id = "mem_stub"
    version = "1.0.0"
    display_name = "Memory Stub Puller"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "title": "URL"},
            "token": {"type": "string", "format": "password", "title": "Token"},
        },
        "required": ["url", "token"],
        "additionalProperties": False,
    }

    def __init__(self, handler):
        self._handler = handler

    def pull(self, context: PullContext) -> PullArtifact:
        return self._handler(context)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)

    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    state = {
        "artifact": PullArtifact(ossie_models={"m.yaml": b"name: m\n"}, table_count=1)
    }
    stub = _MemoryStubPuller(
        lambda ctx: (
            state["artifact"](ctx) if callable(state["artifact"]) else state["artifact"]
        )
    )
    base = dict(plugins.discover())
    base[("puller", "mem_stub")] = stub
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))
    monkeypatch.setattr(pull_instances, "discover", lambda: dict(base))
    monkeypatch.setattr(pull_service, "discover", lambda: dict(base))

    with TestClient(app, headers=_headers(token)) as client:
        yield client, token, state, tmp_path


def test_pull_lifecycle_isolation_noop_circuit_breaker_carry_forward_and_cleanup(env):
    c, _token, state, _tmp = env

    # Create warehouse and document projects
    wh = c.post(
        "/api/v1/projects", json={"name": "Warehouse Lifecycle", "type": "warehouse"}
    ).json()
    doc = c.post(
        "/api/v1/projects", json={"name": "Docs Lifecycle", "type": "document"}
    ).json()
    pid = wh["id"]

    # Document project -> 422
    assert (
        c.post(f"/api/v1/projects/{doc['id']}/pull-instances/inst-a/pull").status_code
        == 422
    )
    # Missing instance -> 404
    assert (
        c.post(f"/api/v1/projects/{pid}/pull-instances/missing/pull").status_code == 404
    )

    # Create two pull instances on warehouse project
    for inst_id, prefix, ns in (
        ("inst-a", "lake_a", "ns.a"),
        ("inst-b", "lake_b", "ns.b"),
    ):
        created = c.post(
            f"/api/v1/projects/{pid}/pull-instances",
            json={
                "id": inst_id,
                "plugin_id": "mem_stub",
                "display_name": f"Instance {inst_id}",
                "parameters": {"url": "https://stub.local", "token": "secret-tok"},
                "catalog_prefix": prefix,
                "task_namespace": ns,
                "drop_threshold": 0.5,
            },
        )
        assert created.status_code == 201, created.text

    # Seed manual Markdown, manual top-level Ossie, overrides, and sibling inst-b files
    src_dir = Path(project_context.source_directory_value(pid))
    with source_directory.repository(src_dir) as root:
        (root / "knowledge" / "biz").mkdir(parents=True)
        (root / "knowledge" / "biz" / "metrics.md").write_text(
            "# Manual Metrics\n", encoding="utf-8"
        )
        (root / "ossie" / "inst-b").mkdir(parents=True)
        (root / "ossie" / "manual_model.yaml").write_text(
            "name: manual\n", encoding="utf-8"
        )
        (root / "ossie" / "inst-b" / "other.yaml").write_text(
            "name: other\n", encoding="utf-8"
        )
        (root / "pulls" / "inst-b").mkdir(parents=True)
        (root / "pulls" / "inst-b" / "tasks.json").write_text(
            '[{"name": "ns.b.t1", "sql": "SELECT 1"}]\n', encoding="utf-8"
        )
        (root / "overrides").mkdir(parents=True)
        (root / "overrides" / "semantic_overrides.yaml").write_text(
            "version: 1\n", encoding="utf-8"
        )
        source_directory.git(root, "add", "-A")
        source_directory.git(root, "commit", "-m", "manual seed", author="Admin")

    # 1. First pull (zero history) -> full build + isolated files + secrets passed in PullContext
    seen_contexts: list[PullContext] = []

    def first_pull(ctx: PullContext) -> PullArtifact:
        seen_contexts.append(ctx)
        return PullArtifact(
            ossie_models={"model_a.yaml": b"name: model_a_v1\n"},
            tasks=[{"name": "ns.a.t_orders", "sql": "SELECT 1", "dialect": "spark"}],
            dimension_values=[
                {
                    "database": "dimensions",
                    "table": "dim_city",
                    "field": "city_code",
                    "value": "SHA",
                },
                {
                    "database": "dimensions",
                    "table": "dim_channel",
                    "field": "ch",
                    "value": "APP",
                },
            ],
            table_count=10,
            task_count=1,
        )

    state["artifact"] = first_pull
    r1 = c.post(f"/api/v1/projects/{pid}/pull-instances/inst-a/pull")
    assert r1.status_code == 200, r1.text
    p1 = r1.json()
    assert p1["instance_id"] == "inst-a"
    assert p1["plugin_id"] == "mem_stub"
    assert p1["trigger_type"] == "manual"
    assert p1["status"] == "succeeded"
    assert p1["skipped_reason"] is None
    assert p1["warnings"] == []
    assert p1["tables"] == 10
    assert p1["tasks"] == 1
    assert p1["commit"] is not None
    assert p1["build_id"] is not None
    assert seen_contexts[0].secrets == {"token": "secret-tok"}
    assert seen_contexts[0].catalog_prefix == "lake_a"
    assert seen_contexts[0].task_namespace == "ns.a"

    # Zero-history rule: first build must be "full"
    b1 = c.get(f"/api/v1/builds/{p1['build_id']}", params={"project": pid}).json()
    assert b1["build_type"] == "full"

    # Manual & sibling files completely untouched
    assert (src_dir / "knowledge" / "biz" / "metrics.md").read_text(
        encoding="utf-8"
    ) == "# Manual Metrics\n"
    assert (src_dir / "ossie" / "manual_model.yaml").read_text(
        encoding="utf-8"
    ) == "name: manual\n"
    assert (src_dir / "overrides" / "semantic_overrides.yaml").read_text(
        encoding="utf-8"
    ) == "version: 1\n"
    assert (src_dir / "ossie" / "inst-b" / "other.yaml").read_text(
        encoding="utf-8"
    ) == "name: other\n"
    assert (src_dir / "pulls" / "inst-b" / "tasks.json").exists()
    assert (
        src_dir / "ossie" / "inst-a" / "model_a.yaml"
    ).read_bytes() == b"name: model_a_v1\n"
    assert (src_dir / "pulls" / "inst-a" / "tasks.json").is_file()
    assert (src_dir / "pulls" / "inst-a" / "dimension_values.json").is_file()

    # 2. No-op skip: identical artifact -> no commit, no build, skipped_reason="no_changes"
    commits_before = int(
        source_directory.git(src_dir, "rev-list", "--count", "HEAD").decode().strip()
    )
    r_noop = c.post(f"/api/v1/projects/{pid}/pull-instances/inst-a/pull")
    assert r_noop.status_code == 200, r_noop.text
    p_noop = r_noop.json()
    assert p_noop["status"] == "succeeded"
    assert p_noop["skipped_reason"] == "no_changes"
    assert p_noop["commit"] is None
    assert p_noop["build_id"] is None
    assert (
        int(
            source_directory.git(src_dir, "rev-list", "--count", "HEAD")
            .decode()
            .strip()
        )
        == commits_before
    )

    # Mark first full build succeeded so next changed pull enqueues "incremental"
    with sessions().begin() as db:
        row_b1 = db.get(Build, p1["build_id"])
        row_b1.status = "succeeded"
        row_b1.finished_at = datetime.now(UTC).isoformat()

    # 3. Partial dimension table failure carry-forward + changed model -> incremental build
    state["artifact"] = lambda _ctx: PullArtifact(
        ossie_models={"model_a.yaml": b"name: model_a_v2\n"},
        tasks=[{"name": "ns.a.t_orders", "sql": "SELECT 2", "dialect": "spark"}],
        dimension_values=[
            {
                "database": "dimensions",
                "table": "dim_channel",
                "field": "ch",
                "value": "WEB",
            }
        ],
        table_count=10,
        task_count=1,
        failures=[
            PullItemFailure(
                kind="dimension_table", target="dim_city", reason="query_timeout"
            )
        ],
    )
    r_partial = c.post(f"/api/v1/projects/{pid}/pull-instances/inst-a/pull")
    assert r_partial.status_code == 200, r_partial.text
    p_partial = r_partial.json()
    assert p_partial["status"] == "succeeded"
    assert p_partial["skipped_reason"] is None
    assert p_partial["build_id"] is not None
    assert any("dim_city" in w and "沿用" in w for w in p_partial["warnings"])
    b2 = c.get(
        f"/api/v1/builds/{p_partial['build_id']}", params={"project": pid}
    ).json()
    assert b2["build_type"] == "incremental"
    carried_dims = json.loads(
        (src_dir / "pulls" / "inst-a" / "dimension_values.json").read_text(
            encoding="utf-8"
        )
    )
    assert {(r["table"], r["value"]) for r in carried_dims} == {
        ("dim_channel", "WEB"),
        ("dim_city", "SHA"),
    }

    # 4. Circuit breaker: 0 tables -> blocked
    commits_before = int(
        source_directory.git(src_dir, "rev-list", "--count", "HEAD").decode().strip()
    )
    state["artifact"] = lambda _ctx: PullArtifact(
        ossie_models={"model_a.yaml": b"name: empty\n"}, table_count=0
    )
    r_zero = c.post(f"/api/v1/projects/{pid}/pull-instances/inst-a/pull")
    assert r_zero.status_code == 200, r_zero.text
    p_zero = r_zero.json()
    assert p_zero["status"] == "blocked"
    assert p_zero["commit"] is None
    assert p_zero["build_id"] is None
    assert "0 张表" in (p_zero["error"] or "")
    assert (
        int(
            source_directory.git(src_dir, "rev-list", "--count", "HEAD")
            .decode()
            .strip()
        )
        == commits_before
    )

    # 5. Circuit breaker: >50% drop (10 -> 4) -> blocked; force=true bypasses!
    state["artifact"] = lambda _ctx: PullArtifact(
        ossie_models={"model_a.yaml": b"name: shrunk\n"}, table_count=4
    )
    r_drop = c.post(f"/api/v1/projects/{pid}/pull-instances/inst-a/pull")
    assert r_drop.status_code == 200
    p_drop = r_drop.json()
    assert p_drop["status"] == "blocked"
    assert "骤降" in (p_drop["error"] or "")
    assert p_drop["commit"] is None
    assert p_drop["build_id"] is None

    r_force = c.post(
        f"/api/v1/projects/{pid}/pull-instances/inst-a/pull", json={"force": True}
    )
    assert r_force.status_code == 200, r_force.text
    p_force = r_force.json()
    assert p_force["status"] == "succeeded"
    assert p_force["trigger_type"] == "forced"
    assert p_force["tables"] == 4
    assert p_force["commit"] is not None
    assert (
        src_dir / "ossie" / "inst-a" / "model_a.yaml"
    ).read_bytes() == b"name: shrunk\n"
    with sessions()() as db:
        assert db.get(ProjectPullInstance, (pid, "inst-a")).last_table_count == 4

    # 6. Deleting instance removes only its own ossie/<id> and pulls/<id>
    assert c.delete(f"/api/v1/projects/{pid}/pull-instances/inst-a").status_code == 204
    assert not (src_dir / "ossie" / "inst-a").exists()
    assert not (src_dir / "pulls" / "inst-a").exists()
    assert (src_dir / "ossie" / "manual_model.yaml").exists()
    assert (src_dir / "ossie" / "inst-b" / "other.yaml").exists()
    assert (src_dir / "pulls" / "inst-b" / "tasks.json").exists()
    assert (src_dir / "knowledge" / "biz" / "metrics.md").exists()
    assert (src_dir / "overrides" / "semantic_overrides.yaml").exists()

    # 7. Archived project -> 409
    assert c.post(f"/api/v1/projects/{pid}/archive").status_code == 200
    assert (
        c.post(f"/api/v1/projects/{pid}/pull-instances/inst-b/pull").status_code == 409
    )


def test_concurrent_instance_pull_returns_409(env):
    c, _token, state, tmp = env
    entered = threading.Event()
    release = threading.Event()

    def slow_pull(_ctx: PullContext) -> PullArtifact:
        entered.set()
        assert release.wait(10)
        return PullArtifact(ossie_models={"m.yaml": b"name: slow\n"}, table_count=2)

    assert (
        c.put(
            "/api/v1/settings",
            json={"values": {"source_directory": str(tmp / "default-source")}},
        ).status_code
        == 200
    )
    state["artifact"] = slow_pull
    assert (
        c.post(
            "/api/v1/projects/default/pull-instances",
            json={
                "id": "slow-inst",
                "plugin_id": "mem_stub",
                "display_name": "Slow",
                "parameters": {"url": "https://stub.local", "token": "tok"},
            },
        ).status_code
        == 201
    )

    results: list[int] = []
    t = threading.Thread(
        target=lambda: results.append(
            c.post("/api/v1/projects/default/pull-instances/slow-inst/pull").status_code
        )
    )
    t.start()
    try:
        assert entered.wait(10)
        conflict = c.post("/api/v1/projects/default/pull-instances/slow-inst/pull")
        assert conflict.status_code == 409
    finally:
        release.set()
        t.join(10)
    assert results == [200]


@pytest.mark.parametrize("point", ["replace", "add", "commit"])
def test_interrupted_pull_instance_recovers_without_duplicate_commit(
    tmp_path: Path, point: str
):
    db_path = tmp_path / "app.db"
    key = Fernet.generate_key().decode()
    repo_path = tmp_path / "source"
    os.environ["DATAAGENT_DB_PATH"] = str(db_path)
    os.environ["DATAAGENT_ENCRYPTION_KEY"] = key

    from semantica_dataagent.cli import initialize

    initialize()
    with source_directory.repository(repo_path) as root:
        (root / "ossie" / "inst-r").mkdir(parents=True)
        (root / "ossie" / "inst-r" / "model.yaml").write_text(
            "name: prior\n", encoding="utf-8"
        )
        (root / "ossie" / "manual.yaml").write_text("name: manual\n", encoding="utf-8")
        (root / "pulls" / "inst-r").mkdir(parents=True)
        (root / "pulls" / "inst-r" / "tasks.json").write_text(
            '[{"name": "old_task", "sql": "SELECT 1"}]\n', encoding="utf-8"
        )
        source_directory.git(root, "add", "-A")
        source_directory.git(root, "commit", "-m", "initial", author="tester")
        before = source_directory.git(root, "rev-parse", "HEAD").decode().strip()

    op_id = f"crash-op-{point}"
    with sessions().begin() as db:
        db.add(
            ProjectPullInstance(
                project_id="default",
                id="inst-r",
                plugin_id="mem_stub",
                display_name="Recover Inst",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        rec = PullRecord(
            project_id="default",
            instance_id="inst-r",
            plugin_id="mem_stub",
            trigger_type="manual",
            status="running",
            started_at=datetime.now(UTC),
            tables=5,
            tasks=1,
            source_directory=str(repo_path),
            operation_id=op_id,
        )
        db.add(rec)
        db.flush()
        rec_id = rec.id

    script = """
import os, sys, signal
from semantica_dataagent import source_directory as source

repo_path, point, op_id = sys.argv[1], sys.argv[2], sys.argv[3]
original_git = source.git
original_replace = source.os.replace

def git(root, *args, **kwargs):
    result = original_git(root, *args, **kwargs)
    if args[0] == point:
        os.kill(os.getpid(), signal.SIGKILL)
    return result

def replace(src, dst):
    original_replace(src, dst)
    if point == "replace" and ".pull-" in str(src):
        os.kill(os.getpid(), signal.SIGKILL)

source.git = git
source.os.replace = replace
with source.repository(repo_path) as root:
    source.publish_pull_instance(
        root,
        "inst-r",
        {"model.yaml": b"name: updated\\n"},
        tasks=[{"name": "new_task", "sql": "SELECT 2"}],
        dimension_values=[{"database": "d", "table": "t", "field": "f", "value": "v"}],
        operation_id=op_id,
    )
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(repo_path), point, op_id],
        env={
            **os.environ,
            "DATAAGENT_DB_PATH": str(db_path),
            "DATAAGENT_ENCRYPTION_KEY": key,
        },
        check=False,
    )
    assert child.returncode == -9

    pull_service.recover_pulls()
    pull_service.recover_pulls()

    with source_directory.repository(repo_path) as root:
        head = source_directory.git(root, "rev-parse", "HEAD").decode().strip()
        assert (root / "ossie" / "manual.yaml").read_text(
            encoding="utf-8"
        ) == "name: manual\n"
        assert not source_directory.git(root, "diff", "--cached", "--name-only")
        assert not (root / ".git" / "dataagent-pull.json").exists()
        assert not (root / ".git" / "dataagent-pull-backup").exists()
        if point == "commit":
            assert head != before
            assert (
                source_directory.git(root, "rev-list", "--count", "HEAD").strip()
                == b"2"
            )
            assert (root / "ossie" / "inst-r" / "model.yaml").read_text(
                encoding="utf-8"
            ) == "name: updated\n"
            assert (root / "pulls" / "inst-r" / "dimension_values.json").is_file()
        else:
            assert head == before
            assert (
                source_directory.git(root, "rev-list", "--count", "HEAD").strip()
                == b"1"
            )
            assert (root / "ossie" / "inst-r" / "model.yaml").read_text(
                encoding="utf-8"
            ) == "name: prior\n"
            assert not (root / "pulls" / "inst-r" / "dimension_values.json").exists()

    with sessions()() as db:
        row = db.get(PullRecord, rec_id)
        if point == "commit":
            assert row.status == "succeeded"
            assert row.commit == head
            assert row.build_id is not None
            assert db.get(Build, row.build_id).build_type == "full"
            assert (
                db.get(ProjectPullInstance, ("default", "inst-r")).last_table_count == 5
            )
        else:
            assert row.status == "failed"
            assert row.build_id is None
