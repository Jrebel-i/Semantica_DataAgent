"""Tests for built-in ossie puller, crash-safe publishing, and project APIs (#278)."""

from __future__ import annotations

import io
import json
import re

import httpx
from openpyxl import Workbook

METADATA_COLUMNS = [
    "table_id",
    "table_name",
    "database_name",
    "create_time",
    "modify_time",
    "comment",
    "output_task_id",
    "output_task_name",
    "use_direction",
    "column_name",
    "seq_no",
    "column_type",
    "column_comment",
    "column_business_comment",
]
PARTITION_HEADER = (
    "table_id,table_name,database_name,create_time,modify_time,comment,"
    "partition_id,partition_name,records,partition_create_time,rk"
)


def _metadata_row(
    table_id: int,
    table_name: str,
    column_name: str,
    seq_no: int,
    column_type: str,
    comment: str = "",
) -> list:
    return [
        str(table_id),
        table_name,
        "dimensions",
        None,
        None,
        comment,
        None,
        None,
        None,
        column_name,
        seq_no,
        column_type,
        comment,
        None,
    ]


def _table_row(
    tid: int,
    db: str,
    tbl: str,
    col: str,
    ctype: str,
    seq: int = 1,
    comment: str = "",
    col_comment: str = "",
    output_task_id: str = "",
) -> list:
    return [tid, tbl, db, comment, output_task_id, "", col, seq, ctype, col_comment, ""]


class _FakeDtb:
    """Minimal fake DTB HTTP transport matching DtbPuller protocol."""

    def __init__(
        self,
        *,
        metadata_rows: list[list],
        table_rows: list[list],
        dimension_rows: dict[str, list[dict]],
        stats: str = PARTITION_HEADER + "\n",
        task_rows: list[list] | None = None,
    ):
        self.metadata_rows = metadata_rows
        self.table_rows = table_rows
        self.dimension_rows = dimension_rows
        self.stats = stats
        self.task = json.dumps(
            {
                "columns": [
                    {"name": "task_name"},
                    {"name": "task_type"},
                    {"name": "task_src"},
                ],
                "data": task_rows or [],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    @staticmethod
    def _key(sql: str) -> str:
        if "ORDER BY a.table_id" in sql:
            return "metadata"
        if "task_version" in sql:
            return "task"
        if "metadata_partition" in sql:
            return "stats"
        match = re.search(r"hive\.dimensions\.([A-Za-z_][A-Za-z0-9_]*)", sql)
        if match:
            return "dim_" + match.group(1)
        return "table"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/data-query/async-execute":
            body = json.loads(request.content)
            key = self._key(body["sql"])
            return httpx.Response(
                200, json={"status": "200", "data": {"queryKey": key}}
            )
        if request.url.path.startswith("/api/data-query/result/"):
            return httpx.Response(
                200, json={"status": "200", "data": {"progress": 1.0}}
            )
        if request.url.path.startswith("/api/data-query/download/"):
            key = request.url.path.rsplit("/", 1)[-1]
            if key == "metadata":
                payload = json.dumps(
                    {
                        "columns": [
                            {"name": c, "type": "VARCHAR"} for c in METADATA_COLUMNS
                        ],
                        "data": self.metadata_rows,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                return httpx.Response(
                    200, content=payload, headers={"content-type": "application/json"}
                )
            if key == "task":
                return httpx.Response(
                    200, content=self.task, headers={"content-type": "application/json"}
                )
            if key == "stats":
                return httpx.Response(
                    200,
                    content=self.stats.encode("utf-8"),
                    headers={"content-type": "text/csv"},
                )
            if key == "table":
                book = Workbook()
                book.active.append(
                    [
                        "table_id",
                        "table_name",
                        "database_name",
                        "comment",
                        "output_task_id",
                        "output_task_name",
                        "column_name",
                        "seq_no",
                        "column_type",
                        "column_comment",
                        "column_business_comment",
                    ]
                )
                for row in self.table_rows:
                    book.active.append(row)
                buf = io.BytesIO()
                book.save(buf)
                return httpx.Response(200, content=buf.getvalue())
            if key.startswith("dim_"):
                tname = key[4:]
                rows = self.dimension_rows.get(tname, [])
                cols = list(rows[0].keys()) if rows else []
                payload = json.dumps(
                    {
                        "columns": [{"name": c, "type": "VARCHAR"} for c in cols],
                        "data": [[r[c] for c in cols] for r in rows],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                return httpx.Response(
                    200, content=payload, headers={"content-type": "application/json"}
                )
        return httpx.Response(404)


def test_dtb_puller_plugin_is_registered_and_transforms_fake_dtb_response(monkeypatch):
    from semantica_dataagent.plugins import PullContext, discover

    found = discover()
    assert ("puller", "dtb") in found
    assert ("puller", "ossie") not in found
    puller = found[("puller", "dtb")]
    assert puller.id == "dtb"
    assert puller.display_name == "DTB"
    assert "interval_seconds" not in puller.parameters_schema["properties"]
    assert {
        "url",
        "token",
        "layer",
        "max_tables",
        "model_name",
        "low_cardinality_limit",
        "sample_limit",
        "dimension_row_limit",
    } <= set(puller.parameters_schema["properties"])

    dim_city_rows = [
        {"city_code": f"C{i:02d}", "city_name": f"城市_{i:02d}"} for i in range(1, 8)
    ]
    fake = _FakeDtb(
        metadata_rows=[
            _metadata_row(1, "dim_city", "city_code", 1, "string", "城市编码"),
            _metadata_row(1, "dim_city", "city_name", 2, "string", "城市名称"),
            _metadata_row(2, "dim_flag", "flag_code", 1, "string", "标记编码"),
        ],
        table_rows=[
            _table_row(
                1,
                "dimensions",
                "dim_city",
                "city_code",
                "string",
                1,
                "城市维表",
                "城市编码",
            ),
            _table_row(
                1,
                "dimensions",
                "dim_city",
                "city_name",
                "string",
                2,
                "城市维表",
                "城市名称",
            ),
            _table_row(
                2,
                "dimensions",
                "dim_flag",
                "flag_code",
                "string",
                1,
                "标记维表",
                "标记编码",
            ),
        ],
        dimension_rows={
            "dim_city": dim_city_rows,
            "dim_flag": [{"flag_code": "Y"}, {"flag_code": "N"}],
        },
        task_rows=[
            [
                "t_city",
                "spark sql",
                "INSERT OVERWRITE TABLE dimensions.dim_city SELECT city_code, city_name FROM dimensions.dim_city",
            ]
        ],
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        "semantica_dataagent.dtb_ossie.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=httpx.MockTransport(fake), timeout=30
        ),
    )
    artifact = puller.pull(
        PullContext(
            project_id="p1",
            instance_id="inst1",
            parameters={
                "url": "https://dtb.example",
                "layer": "dimensions",
                "model_name": "dims_pulled",
                "low_cardinality_limit": 5,
                "sample_limit": 3,
            },
            secrets={"token": "secret-token"},
        )
    )
    assert "dims_pulled.yaml" in artifact.ossie_models
    yaml_text = artifact.ossie_models["dims_pulled.yaml"].decode("utf-8")
    assert "name: dims_pulled" in yaml_text
    assert "示例取值（共 7 个去重值，抽样前 3 个）" in yaml_text
    assert "全部取值（共 2 个）" in yaml_text
    assert len(artifact.dimension_values) == 14
    assert {r["table"] for r in artifact.dimension_values} == {"dim_city"}
    assert artifact.table_count == 2
    assert artifact.task_count == 1
    assert artifact.stats["dimension_values"] == 14


def test_publish_pull_instance_protects_user_files_and_cleans_stale_pull_artifacts(
    tmp_path,
):
    from semantica_dataagent import source_directory

    with source_directory.repository(tmp_path / "source") as root:
        # User commits manual Ossie model & manual dimension_values.json
        (root / "ossie").mkdir()
        (root / "ossie" / "manual_model.yaml").write_text(
            "name: manual\n", encoding="utf-8"
        )
        (root / "dimension_values.json").write_text(
            '[{"custom": true}]', encoding="utf-8"
        )
        source_directory.git(
            root, "add", "ossie/manual_model.yaml", "dimension_values.json"
        )
        source_directory.git(root, "commit", "-m", "user upload", author="Alice")

        # Pull 1 writes ossie/inst-1/dtb_semantic_model.yaml
        c1, changed1 = source_directory.publish_pull_instance(
            root,
            "inst-1",
            {"dtb_semantic_model.yaml": b"name: auto_v1\n"},
            operation_id="op-1",
        )
        assert changed1 is True
        assert (root / "ossie" / "manual_model.yaml").read_text(
            encoding="utf-8"
        ) == "name: manual\n"
        assert (root / "dimension_values.json").read_text(
            encoding="utf-8"
        ) == '[{"custom": true}]'
        assert (
            source_directory.git(root, "show", "--format=%an", "--no-patch", c1).strip()
            == "定时拉取".encode()
        )

        # Pull 2 switches model_name and emits dimension_values.json
        source_directory.publish_pull_instance(
            root,
            "inst-1",
            {"domain_v2.yaml": b"name: domain_v2\n"},
            dimension_values=[
                {"database": "dimensions", "table": "t", "field": "f", "value": "1"}
            ],
            operation_id="op-2",
        )
        assert not (root / "ossie" / "inst-1" / "dtb_semantic_model.yaml").exists()
        assert (root / "ossie" / "manual_model.yaml").exists()
        assert (root / "ossie" / "inst-1" / "domain_v2.yaml").read_text(
            encoding="utf-8"
        ) == "name: domain_v2\n"
        assert (root / "pulls" / "inst-1" / "dimension_values.json").is_file()

        # Pull 3 omits dimension_values -> stale pulls/inst-1/dimension_values.json IS cleaned
        source_directory.publish_pull_instance(
            root,
            "inst-1",
            {"domain_v2.yaml": b"name: domain_v3\n"},
            operation_id="op-3",
        )
        assert not (root / "pulls" / "inst-1" / "dimension_values.json").exists()
        assert (root / "ossie" / "manual_model.yaml").exists()

        # Staged changes must abort cleanly
        (root / "note.txt").write_text("staged", encoding="utf-8")
        source_directory.git(root, "add", "note.txt")
        import pytest

        with pytest.raises(ValueError, match="staged"):
            source_directory.publish_pull_instance(
                root,
                "inst-1",
                {"domain_v2.yaml": b"name: rejected\n"},
            )
        assert (root / "ossie" / "inst-1" / "domain_v2.yaml").read_text(
            encoding="utf-8"
        ) == "name: domain_v3\n"


def test_interrupted_pull_instance_recovers_overwrite_without_loss_or_duplicate_commit(
    tmp_path,
):
    import os
    import subprocess
    import sys

    from semantica_dataagent import source_directory

    for point in ("replace", "add", "commit"):
        repo_path = tmp_path / f"source-{point}"
        with source_directory.repository(repo_path) as root:
            (root / "ossie" / "inst-1").mkdir(parents=True)
            (root / "ossie" / "inst-1" / "dtb_semantic_model.yaml").write_text(
                "name: prior_model\n", encoding="utf-8"
            )
            source_directory.git(root, "add", "ossie/inst-1/dtb_semantic_model.yaml")
            source_directory.git(root, "commit", "-m", "initial", author="tester")
            before = source_directory.git(root, "rev-parse", "HEAD").decode().strip()

        script = """
import os, sys, signal
from semantica_dataagent import source_directory as source
point = sys.argv[2]
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
with source.repository(sys.argv[1]) as root:
    source.publish_pull_instance(
        root,
        "inst-1",
        {"dtb_semantic_model.yaml": b"name: next_model\\n"},
        dimension_values=[{"database": "d", "table": "t", "field": "f", "value": "v"}],
        operation_id="crash-op",
    )
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(repo_path), point],
            env=os.environ.copy(),
            check=False,
        )
        assert child.returncode == -9, (
            f"Expected SIGKILL for point={point}, got {child.returncode}"
        )

        with source_directory.repository(repo_path) as root:
            head = source_directory.git(root, "rev-parse", "HEAD").decode().strip()
            yaml_now = (
                root / "ossie" / "inst-1" / "dtb_semantic_model.yaml"
            ).read_text(encoding="utf-8")
            if point == "commit":
                assert head != before
                assert (
                    source_directory.git(root, "rev-list", "--count", "HEAD").strip()
                    == b"2"
                )
                assert yaml_now == "name: next_model\n"
                assert (root / "pulls" / "inst-1" / "dimension_values.json").is_file()
            else:
                assert head == before
                assert yaml_now == "name: prior_model\n"
                assert not (
                    root / "pulls" / "inst-1" / "dimension_values.json"
                ).exists()
            assert not source_directory.git(root, "diff", "--cached", "--name-only")
            assert not (root / ".git" / "dataagent-pull.json").exists()
            assert not (root / ".git" / "dataagent-pull-backup").exists()


def test_project_dtb_pull_instance_lifecycle_and_end_to_end_build(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))

    from semantica_dataagent import knowledge, project_context, sql_validation
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, PullRecord, sessions
    from semantica_dataagent.sql_validation import validate_sql
    from semantica_dataagent.stage_contract import StorageHandles

    OSSIE_SECRET = "ossie-pull-secret-xyz"
    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}

    # Configure fake graph/vector stores for run_build verification
    sql_validation._snapshot_cache = None
    tables_written: list[dict] = []
    fields_written: list[dict] = []
    dims_written: list[dict] = []
    rels_written: list[dict] = []

    class FakeGraph:
        def connect(self):
            pass

        def close(self):
            pass

        def execute_query(self, query: str, parameters: dict | None = None):
            params = parameters or {}
            gen = params.get("generation")
            q = " ".join(query.split())
            if "AS deleted" in q or ("DETACH DELETE" in q and "SDAEntity" in q):
                return {"success": True, "records": [{"deleted": 0}]}
            if "CREATE (n:SDAEntity:SDATable) SET n = row" in q:
                tables_written.extend({**r, "generation": gen} for r in params["rows"])
            elif "CREATE (f:SDAEntity:SDAField) SET f = row" in q:
                fields_written.extend({**r, "generation": gen} for r in params["rows"])
            elif "CREATE (v:SDAEntity:SDADimensionValue) SET v = row" in q:
                dims_written.extend({**r, "generation": gen} for r in params["rows"])
            elif "MERGE (a)-[r:RELATES_TO {ref: row.props.ref}]->(b)" in q:
                rels_written.extend(params["rows"])
            elif "OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField" in q:
                rows = []
                for t in tables_written:
                    if t.get("generation") != gen:
                        continue
                    t_fields = [
                        dict(f)
                        for f in fields_written
                        if f.get("generation") == gen
                        and f["database"] == t["database"]
                        and f["table"] == t["name"]
                    ]
                    rows.append({"table": dict(t), "fields": t_fields, "documents": []})
                return {"success": True, "records": rows}
            elif "-[r:RELATES_TO]->(:SDATable" in q:
                return {
                    "success": True,
                    "records": [
                        {"relationship": dict(r["props"])} for r in rels_written
                    ],
                }
            return {"success": True, "records": []}

    class FakeVectors:
        collection = type("C", (), {"load": lambda self: None})()
        client = type("Cl", (), {"disconnect": lambda self: None})()

        def create_collection(self, *args, **kwargs):
            pass

        def get_collection(self, *args, **kwargs):
            pass

        def add_vectors(self, *args, **kwargs):
            pass

        def delete_vectors(self, *args, **kwargs):
            pass

    graph_store, vector_store = FakeGraph(), FakeVectors()

    @contextmanager
    def fake_storage(values, stats=None):
        yield StorageHandles(
            values["knowledge_generation"],
            graph_store,
            vector_store,
            values,
            _embedding_dimension=4,
        )

    monkeypatch.setattr(knowledge, "build_storage", fake_storage)
    monkeypatch.setattr(knowledge, "incremental_storage", fake_storage)
    monkeypatch.setattr(knowledge, "read_graph", lambda values: graph_store)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: vector_store)
    monkeypatch.setattr(
        knowledge,
        "embed",
        lambda values, texts, stats=None: [[0.1, 0.2, 0.3, 0.4]] * len(texts),
    )
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: None)

    dim_city_rows = [
        {"city_code": f"C{i:02d}", "city_name": f"城市_{i:02d}"} for i in range(1, 8)
    ]
    fake_dtb = _FakeDtb(
        metadata_rows=[
            _metadata_row(1, "dim_city", "city_code", 1, "string", "城市编码"),
            _metadata_row(1, "dim_city", "city_name", 2, "string", "城市名称"),
            _metadata_row(2, "dim_flag", "flag_code", 1, "string", "标记编码"),
        ],
        table_rows=[
            _table_row(
                1,
                "dimensions",
                "dim_city",
                "city_code",
                "string",
                1,
                "城市维表",
                "城市编码",
            ),
            _table_row(
                1,
                "dimensions",
                "dim_city",
                "city_name",
                "string",
                2,
                "城市维表",
                "城市名称",
            ),
            _table_row(
                2,
                "dimensions",
                "dim_flag",
                "flag_code",
                "string",
                1,
                "标记维表",
                "标记编码",
            ),
        ],
        dimension_rows={
            "dim_city": dim_city_rows,
            "dim_flag": [{"flag_code": "Y"}, {"flag_code": "N"}],
        },
        task_rows=[
            [
                "t_city",
                "spark sql",
                "SELECT c.city_code, f.flag_code FROM dimensions.dim_city c JOIN dimensions.dim_flag f ON c.city_code = f.flag_code",
            ]
        ],
    )

    import httpx as _httpx

    real_client_cls = _httpx.Client
    monkeypatch.setattr(
        "semantica_dataagent.dtb_ossie.httpx.Client",
        lambda *args, **kwargs: real_client_cls(
            transport=_httpx.MockTransport(fake_dtb), timeout=30
        ),
    )
    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)

    with TestClient(app) as c:
        assert (
            c.put(
                "/api/v1/settings",
                headers=headers,
                json={
                    "values": {
                        "source_directory": str(tmp_path / "default_src"),
                        "neo4j_uri": "bolt://127.0.0.1:7687",
                        "neo4j_user": "neo4j",
                        "neo4j_password": "secret",
                        "milvus_uri": "http://127.0.0.1:19530",
                        "embedding_url": "http://127.0.0.1:8000",
                        "embedding_model": "test-model",
                    }
                },
            ).status_code
            == 200
        )

        # Create document project -> pull-instances rejected with 422
        doc_proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Docs", "type": "document"},
        ).json()
        assert (
            c.post(
                f"/api/v1/projects/{doc_proj['id']}/pull-instances",
                headers=headers,
                json={
                    "id": "dtb-doc",
                    "plugin_id": "dtb",
                    "display_name": "DTB",
                    "parameters": {"url": "https://dtb.example", "token": "t"},
                },
            ).status_code
            == 422
        )

        # Create warehouse project for topic domain pull
        proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "TopicDomain", "type": "warehouse"},
        ).json()
        pid = proj["id"]
        assert (
            c.put(
                f"/api/v1/projects/{pid}/adapters",
                headers=headers,
                json={
                    "adapters": [
                        {
                            "id": "ossie",
                            "enabled": True,
                            "parameters": {
                                "path": "ossie",
                                "category": "topic_ossie",
                            },
                        }
                    ]
                },
            ).status_code
            == 200
        )

        created = c.post(
            f"/api/v1/projects/{pid}/pull-instances",
            headers=headers,
            json={
                "id": "dims-main",
                "plugin_id": "dtb",
                "display_name": "Dims Main",
                "parameters": {
                    "url": "https://dtb.example",
                    "layer": "dimensions",
                    "model_name": "city_domain",
                    "low_cardinality_limit": 5,
                    "sample_limit": 3,
                    "token": OSSIE_SECRET,
                },
            },
        )
        assert created.status_code == 201, created.text
        assert OSSIE_SECRET not in created.text

        # Sending masked "***" preserves stored token
        updated = c.put(
            f"/api/v1/projects/{pid}/pull-instances/dims-main",
            headers=headers,
            json={
                "parameters": {
                    "url": "https://dtb.example",
                    "layer": "dimensions",
                    "model_name": "city_domain",
                    "low_cardinality_limit": 5,
                    "sample_limit": 3,
                    "token": "***",
                }
            },
        )
        assert updated.status_code == 200, updated.text
        import os
        from pathlib import Path

        assert (
            OSSIE_SECRET.encode()
            not in Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes()
        )

        # First pull on empty project -> writes ossie/dims-main/city_domain.yaml + pulls/dims-main/{tasks,dimension_values}.json and enqueues FULL build!
        p1 = c.post(
            f"/api/v1/projects/{pid}/pull-instances/dims-main/pull",
            headers=headers,
        )
        assert p1.status_code == 200, p1.text
        p1_json = p1.json()
        assert p1_json["status"] == "succeeded", p1_json
        assert p1_json["started_at"].endswith("Z"), p1_json
        with sessions()() as db:
            b1 = db.get(Build, p1_json["build_id"])
            assert b1.build_type == "full"
            assert b1.triggered_by == "定时拉取"

        proj_src = Path(project_context.source_directory_value(pid))
        assert (proj_src / "ossie" / "dims-main" / "city_domain.yaml").is_file()
        assert (proj_src / "pulls" / "dims-main" / "tasks.json").is_file()
        assert (proj_src / "pulls" / "dims-main" / "dimension_values.json").is_file()

        # Execute the queued full build -> succeeds, writes tables/rels/high-cardinality dim values
        knowledge.run_build(p1_json["build_id"])
        sql_validation._snapshot_cache = None
        with sessions()() as db:
            b1_done = db.get(Build, p1_json["build_id"])
            assert b1_done.status == "succeeded", b1_done.error
        assert {d["table"] for d in dims_written} == {"dim_city"}
        val_res = validate_sql(
            "SELECT c.city_code FROM dimensions.dim_city c JOIN dimensions.dim_flag f ON c.city_code = f.flag_code",
            "doris",
            project_id=pid,
        )
        assert val_res["status"] == "pass", val_res

        # Second pull with changed model after successful full build -> enqueues INCREMENTAL build!
        fake_dtb.task = json.dumps(
            {
                "columns": [
                    {"name": "task_name"},
                    {"name": "task_type"},
                    {"name": "task_src"},
                ],
                "data": [
                    [
                        "t_city",
                        "spark sql",
                        "SELECT c.city_code FROM dimensions.dim_city c",
                    ]
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        p2 = c.post(
            f"/api/v1/projects/{pid}/pull-instances/dims-main/pull",
            headers=headers,
        )
        assert p2.status_code == 200, p2.text
        with sessions()() as db:
            b2 = db.get(Build, p2.json()["build_id"])
            assert b2.build_type == "incremental"

        # Archived project rejects pull with 409
        assert (
            c.post(f"/api/v1/projects/{pid}/archive", headers=headers).status_code
            == 200
        )
        with sessions()() as db:
            before_cnt = db.query(PullRecord).count()
        arch_resp = c.post(
            f"/api/v1/projects/{pid}/pull-instances/dims-main/pull",
            headers=headers,
        )
        assert arch_resp.status_code == 409
        assert "项目已归档" in arch_resp.text
        with sessions()() as db:
            assert db.query(PullRecord).count() == before_cnt


def test_puller_plugin_pull_returns_validated_artifact_with_prefixes(monkeypatch):
    from pathlib import Path

    import yaml
    from jsonschema import validate

    from semantica_dataagent.dtb_ossie import DtbPluginPuller
    from semantica_dataagent.ossie_export import validate_ossie_document
    from semantica_dataagent.plugins import PullArtifact, PullContext

    task_schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "schemas" / "v2" / "task.schema.json"
        ).read_text(encoding="utf-8")
    )
    dim_schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "v2"
            / "dimension_value.schema.json"
        ).read_text(encoding="utf-8")
    )

    dim_city_rows = [
        {"city_code": f"C{i:02d}", "city_name": f"城市_{i:02d}"} for i in range(1, 8)
    ]
    fake = _FakeDtb(
        metadata_rows=[
            _metadata_row(1, "dim_city", "city_code", 1, "string", "城市编码"),
            _metadata_row(1, "dim_city", "city_name", 2, "string", "城市名称"),
            _metadata_row(2, "dim_flag", "flag_code", 1, "string", "标记编码"),
        ],
        table_rows=[
            _table_row(
                1,
                "dimensions",
                "dim_city",
                "city_code",
                "string",
                1,
                "城市维表",
                "城市编码",
            ),
            _table_row(
                1,
                "dimensions",
                "dim_city",
                "city_name",
                "string",
                2,
                "城市维表",
                "城市名称",
            ),
            _table_row(
                2,
                "dimensions",
                "dim_flag",
                "flag_code",
                "string",
                1,
                "标记维表",
                "标记编码",
            ),
        ],
        dimension_rows={
            "dim_city": dim_city_rows,
            "dim_flag": [{"flag_code": "Y"}, {"flag_code": "N"}],
        },
        task_rows=[
            [
                "t_city",
                "spark sql",
                "INSERT OVERWRITE TABLE dimensions.dim_city SELECT city_code, city_name FROM dimensions.dim_city",
            ]
        ],
    )
    real_client = httpx.Client
    monkeypatch.setattr(
        "semantica_dataagent.dtb_ossie.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=httpx.MockTransport(fake), timeout=30
        ),
    )

    ctx = PullContext(
        project_id="proj_1",
        instance_id="inst_a",
        parameters={
            "url": "https://dtb.example",
            "layer": "dimensions",
            "model_name": "city_model",
            "low_cardinality_limit": 5,
            "sample_limit": 2,
            "dimension_row_limit": 250,
        },
        secrets={"token": "secret-token"},
        catalog_prefix="lake_a",
        task_namespace="ns_a",
    )

    plugin = DtbPluginPuller()
    artifact = plugin.pull(ctx)
    assert isinstance(artifact, PullArtifact)
    assert set(artifact.ossie_models) == {"city_model.yaml"}
    parsed_model = yaml.safe_load(
        artifact.ossie_models["city_model.yaml"].decode("utf-8")
    )
    validate_ossie_document(parsed_model)
    sources = {ds["source"] for ds in parsed_model["datasets"]}
    assert sources == {"lake_a.dimensions.dim_city", "lake_a.dimensions.dim_flag"}
    city_ds = next(
        ds
        for ds in parsed_model["datasets"]
        if ds["source"] == "lake_a.dimensions.dim_city"
    )
    code_field = next(f for f in city_ds["fields"] if f["name"] == "city_code")
    assert "抽样前 2 个" in code_field["ai_context"]["instructions"]

    assert artifact.task_count == 1
    assert len(artifact.tasks) == 1
    validate(artifact.tasks[0], task_schema)
    assert artifact.tasks[0]["name"] == "ns_a:t_city"

    assert artifact.table_count == 2
    assert len(artifact.dimension_values) == 14
    for item in artifact.dimension_values:
        validate(item, dim_schema)
        assert item["row_id"].startswith("lake_a.dimensions.dim_city:")
    assert artifact.failures == []
    assert all(isinstance(v, int) for v in artifact.stats.values())


def test_puller_plugin_validates_credentials_limits_and_partial_failures(monkeypatch):
    import pytest

    from semantica_dataagent.dtb_ossie import DtbPluginPuller
    from semantica_dataagent.dtb_puller import DtbPuller
    from semantica_dataagent.plugins import PullContext, PullItemFailure

    plugin = DtbPluginPuller()
    with pytest.raises(ValueError, match="token"):
        plugin.pull(
            PullContext(
                project_id="p1",
                instance_id="i1",
                parameters={"url": "https://dtb.example"},
                secrets={},
            )
        )
    with pytest.raises(ValueError, match="URL"):
        plugin.pull(
            PullContext(
                project_id="p1",
                instance_id="i1",
                parameters={"url": "ftp://dtb.example"},
                secrets={"token": "tok"},
            )
        )
    with pytest.raises(ValueError):
        plugin.pull(
            PullContext(
                project_id="p1",
                instance_id="i1",
                parameters={
                    "url": "https://dtb.example",
                    "dimension_row_limit": 500001,
                },
                secrets={"token": "tok"},
            )
        )

    observed_sqls: list[str] = []
    fake = _FakeDtb(
        metadata_rows=[
            # dim_bad has 2 partition columns -> triggers multiple_partition_fields failure
            _metadata_row(1, "dim_bad", "day_num", 10001, "string"),
            _metadata_row(1, "dim_bad", "hour_num", 10002, "string"),
            _metadata_row(2, "dim_good", "code", 1, "string"),
        ],
        table_rows=[
            _table_row(1, "dimensions", "dim_bad", "day_num", "string", 10001),
            _table_row(1, "dimensions", "dim_bad", "hour_num", "string", 10002),
            _table_row(2, "dimensions", "dim_good", "code", "string", 1),
        ],
        dimension_rows={"dim_good": [{"code": "A"}, {"code": "B"}]},
        stats=(
            PARTITION_HEADER
            + "\n1,dim_bad,dimensions,,,bad,,day_num=20261105,1,,1\n"
            + "2,dim_good,dimensions,,,good,,,0,,1\n"
        ),
        task_rows=[["t_good", "spark sql", "SELECT code FROM dimensions.dim_good"]],
    )

    def capturing_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/data-query/async-execute":
            observed_sqls.append(json.loads(request.content)["sql"])
        return fake(request)

    real_client = httpx.Client
    monkeypatch.setattr(
        "semantica_dataagent.dtb_ossie.httpx.Client",
        lambda *args, **kwargs: real_client(
            transport=httpx.MockTransport(capturing_transport), timeout=30
        ),
    )

    # Configurable dimension_row_limit overrides legacy 100 cap even when max_tables is set
    with real_client(transport=httpx.MockTransport(capturing_transport)) as client:
        _, dtb_counts = DtbPuller().fetch(
            client,
            "https://dtb.example",
            "tok",
            {"layer": "dimensions", "max_tables": 2, "dimension_row_limit": 250},
        )
        assert dtb_counts["failures"] == [
            {"table": "dim_bad", "reason": "multiple_partition_fields"}
        ]
    assert any("LIMIT 250" in sql for sql in observed_sqls)

    observed_sqls.clear()
    artifact = plugin.pull(
        PullContext(
            project_id="p1",
            instance_id="i1",
            parameters={
                "url": "https://dtb.example",
                "max_tables": 2,
                "dimension_row_limit": 321,
            },
            secrets={"token": "tok"},
        )
    )
    assert any("LIMIT 321" in sql for sql in observed_sqls)
    assert artifact.failures == [
        PullItemFailure(
            kind="dimension_table",
            target="dim_bad",
            reason="multiple_partition_fields",
        )
    ]
    assert artifact.task_count == 1
    assert artifact.table_count == 2
