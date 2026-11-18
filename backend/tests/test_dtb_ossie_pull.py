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


def test_dtb_ossie_puller_is_registered_and_transforms_fake_dtb_response():
    from semantica_dataagent.plugins import discover

    found = discover()
    assert ("puller", "ossie") in found
    puller = found[("puller", "ossie")]
    assert puller.display_name == "DTB 拉取为 Ossie"
    assert {
        "url",
        "token",
        "interval_seconds",
        "layer",
        "max_tables",
        "model_name",
        "low_cardinality_limit",
        "sample_limit",
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
    client = httpx.Client(transport=httpx.MockTransport(fake))
    files, counts = puller.fetch(
        client,
        "https://dtb.example",
        "secret-token",
        {
            "layer": "dimensions",
            "model_name": "dims_pulled",
            "low_cardinality_limit": 5,
            "sample_limit": 3,
        },
        ossie_path="semantic",
    )
    assert "semantic/dims_pulled.yaml" in files
    assert "dimension_values.json" in files
    yaml_text = files["semantic/dims_pulled.yaml"].decode("utf-8")
    assert "name: dims_pulled" in yaml_text
    assert "示例取值（共 7 个去重值，抽样前 3 个）" in yaml_text
    assert "枚举取值（共 2 个）" in yaml_text
    dim_rows = json.loads(files["dimension_values.json"].decode("utf-8"))
    assert len(dim_rows) == 14
    assert {r["table"] for r in dim_rows} == {"dim_city"}
    assert counts["datasets"] == 2
    assert counts["dimension_values"] == 14


def test_publish_ossie_pull_protects_user_files_and_cleans_stale_pull_artifacts(
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

        # Pull 1 writes dtb_semantic_model.yaml (does not overwrite manual_model.yaml)
        c1 = source_directory.publish_ossie_pull(
            root,
            {
                "ossie/dtb_semantic_model.yaml": b"name: auto_v1\n",
            },
            ossie_path="ossie",
            operation_id="op-1",
        )
        assert (root / "ossie" / "manual_model.yaml").read_text(
            encoding="utf-8"
        ) == "name: manual\n"
        # User-authored dimension_values.json must NOT be deleted!
        assert (root / "dimension_values.json").read_text(
            encoding="utf-8"
        ) == '[{"custom": true}]'
        assert (
            source_directory.git(root, "show", "--format=%an", "--no-patch", c1).strip()
            == "定时拉取".encode()
        )

        # Pull 2 switches model_name from dtb_semantic_model -> domain_v2 and emits pulled dimension_values.json
        source_directory.publish_ossie_pull(
            root,
            {
                "ossie/domain_v2.yaml": b"name: domain_v2\n",
                "dimension_values.json": b'[{"pulled": 1}]',
            },
            ossie_path="ossie",
            operation_id="op-2",
        )
        # Old pull-authored dtb_semantic_model.yaml must be cleaned; manual_model.yaml preserved!
        assert not (root / "ossie" / "dtb_semantic_model.yaml").exists()
        assert (root / "ossie" / "manual_model.yaml").exists()
        assert (root / "ossie" / "domain_v2.yaml").read_text(
            encoding="utf-8"
        ) == "name: domain_v2\n"

        # Pull 3 omits dimension_values.json -> now last author was "定时拉取", so stale pulled file IS cleaned
        source_directory.publish_ossie_pull(
            root,
            {"ossie/domain_v2.yaml": b"name: domain_v3\n"},
            ossie_path="ossie",
            operation_id="op-3",
        )
        assert not (root / "dimension_values.json").exists()
        assert (root / "ossie" / "manual_model.yaml").exists()

        # Staged changes must abort cleanly
        (root / "note.txt").write_text("staged", encoding="utf-8")
        source_directory.git(root, "add", "note.txt")
        import pytest

        with pytest.raises(ValueError, match="staged"):
            source_directory.publish_ossie_pull(
                root,
                {"ossie/domain_v2.yaml": b"name: rejected\n"},
                ossie_path="ossie",
            )
        assert (root / "ossie" / "domain_v2.yaml").read_text(
            encoding="utf-8"
        ) == "name: domain_v3\n"


def test_interrupted_ossie_pull_recovers_overwrite_without_loss_or_duplicate_commit(
    tmp_path,
):
    import os
    import subprocess
    import sys

    from semantica_dataagent import source_directory

    for point in ("replace", "add", "commit"):
        repo_path = tmp_path / f"source-{point}"
        with source_directory.repository(repo_path) as root:
            (root / "ossie").mkdir()
            (root / "ossie" / "dtb_semantic_model.yaml").write_text(
                "name: prior_model\n", encoding="utf-8"
            )
            source_directory.git(root, "add", "ossie/dtb_semantic_model.yaml")
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
    source.publish_ossie_pull(
        root,
        {
            "ossie/dtb_semantic_model.yaml": b"name: next_model\\n",
            "dimension_values.json": b'[{"k": "v"}]',
        },
        ossie_path="ossie",
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
            yaml_now = (root / "ossie" / "dtb_semantic_model.yaml").read_text(
                encoding="utf-8"
            )
            if point == "commit":
                assert head != before
                assert (
                    source_directory.git(root, "rev-list", "--count", "HEAD").strip()
                    == b"2"
                )
                assert yaml_now == "name: next_model\n"
                assert (root / "dimension_values.json").read_text(
                    encoding="utf-8"
                ) == '[{"k": "v"}]'
            else:
                assert head == before
                assert yaml_now == "name: prior_model\n"
                assert not (root / "dimension_values.json").exists()
            assert not source_directory.git(root, "diff", "--cached", "--name-only")
            assert not (root / ".git" / "dataagent-pull.json").exists()
            assert not (root / ".git" / "dataagent-pull-backup").exists()


def test_project_ossie_pull_config_exclusion_lifecycle_and_end_to_end_build(
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
        "semantica_dataagent.pull_service.httpx.Client",
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

        # Create document project -> ossie pull rejected with 422
        doc_proj = c.post(
            "/api/v1/projects",
            headers=headers,
            json={"name": "Docs", "type": "document"},
        ).json()
        assert (
            c.put(
                f"/api/v1/projects/{doc_proj['id']}/adapters",
                headers=headers,
                json={
                    "adapters": [
                        {
                            "id": "ossie",
                            "enabled": False,
                            "parameters": {},
                            "pull": {
                                "enabled": True,
                                "parameters": {"url": "https://dtb.example"},
                            },
                        }
                    ]
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
        adapters_url = f"/api/v1/projects/{pid}/adapters"

        # Enable dtb pull first, then trying to enable ossie pull without disabling dtb -> 422 mutual exclusion
        assert (
            c.put(
                adapters_url,
                headers=headers,
                json={
                    "adapters": [
                        {
                            "id": "dtb",
                            "enabled": True,
                            "parameters": {},
                            "pull": {
                                "enabled": True,
                                "parameters": {"url": "https://dtb.example"},
                            },
                        }
                    ]
                },
            ).status_code
            == 200
        )
        conflict = c.put(
            adapters_url,
            headers=headers,
            json={
                "adapters": [
                    {
                        "id": "ossie",
                        "enabled": True,
                        "parameters": {},
                        "pull": {
                            "enabled": True,
                            "parameters": {"url": "https://dtb.example"},
                        },
                    }
                ]
            },
        )
        assert conflict.status_code == 422, conflict.text
        assert "同时启用" in conflict.text

        # Global /api/v1/plugins/puller/ossie also enforces mutual exclusion with puller/dtb
        assert (
            c.put(
                "/api/v1/plugins/puller/dtb",
                headers=headers,
                json={
                    "enabled": True,
                    "parameters": {"url": "https://d.example", "token": "t1"},
                },
            ).status_code
            == 200
        )
        assert (
            c.put(
                "/api/v1/plugins/puller/ossie",
                headers=headers,
                json={
                    "enabled": True,
                    "parameters": {"url": "https://d.example", "token": "t2"},
                },
            ).status_code
            == 422
        )

        # Atomically disable dtb pull and enable ossie pull + save encrypted token
        enabled = c.put(
            adapters_url,
            headers=headers,
            json={
                "adapters": [
                    {
                        "id": "dtb",
                        "enabled": False,
                        "parameters": {},
                        "pull": {
                            "enabled": False,
                            "parameters": {"url": "https://dtb.example"},
                        },
                    },
                    {
                        "id": "ossie",
                        "enabled": True,
                        "parameters": {
                            "path": "semantic_models",
                            "category": "topic_ossie",
                        },
                        "pull": {
                            "enabled": True,
                            "parameters": {
                                "url": "https://dtb.example",
                                "interval_seconds": 120,
                                "layer": "dimensions",
                                "model_name": "city_domain",
                                "low_cardinality_limit": 5,
                                "sample_limit": 3,
                                "token": OSSIE_SECRET,
                            },
                        },
                    },
                ]
            },
        )
        assert enabled.status_code == 200, enabled.text
        rows_by_id = {row["id"]: row for row in enabled.json()}
        assert rows_by_id["ossie"]["pull"]["enabled"] is True
        assert rows_by_id["ossie"]["pull"]["token_configured"] is True
        assert OSSIE_SECRET not in enabled.text

        # Sending masked "***" must preserve stored token
        assert (
            c.put(
                adapters_url,
                headers=headers,
                json={
                    "adapters": [
                        {
                            "id": "ossie",
                            "enabled": True,
                            "parameters": {
                                "path": "semantic_models",
                                "category": "topic_ossie",
                            },
                            "pull": {
                                "enabled": True,
                                "parameters": {
                                    "url": "https://dtb.example",
                                    "interval_seconds": 120,
                                    "layer": "dimensions",
                                    "model_name": "city_domain",
                                    "low_cardinality_limit": 5,
                                    "sample_limit": 3,
                                    "token": "***",
                                },
                            },
                        }
                    ]
                },
            ).status_code
            == 200
        )
        assert c.get(adapters_url, headers=headers).json()
        import os
        from pathlib import Path

        assert (
            OSSIE_SECRET.encode()
            not in Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes()
        )

        # First pull on empty project -> writes semantic_models/city_domain.yaml + dimension_values.json and enqueues FULL build!
        p1 = c.post("/api/v1/pulls/ossie", headers=headers, params={"project": pid})
        assert p1.status_code == 200, p1.text
        p1_json = p1.json()
        assert p1_json["status"] == "succeeded", p1_json
        assert p1_json["started_at"].endswith("Z"), p1_json
        with sessions()() as db:
            b1 = db.get(Build, p1_json["build_id"])
            assert b1.build_type == "full"
            assert b1.triggered_by == "定时拉取"

        proj_src = Path(project_context.source_directory_value(pid))
        assert (proj_src / "semantic_models" / "city_domain.yaml").is_file()
        assert (proj_src / "dimension_values.json").is_file()

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

        # Second pull after successful full build -> enqueues INCREMENTAL build!
        p2 = c.post("/api/v1/pulls/ossie", headers=headers, params={"project": pid})
        assert p2.status_code == 200, p2.text
        with sessions()() as db:
            b2 = db.get(Build, p2.json()["build_id"])
            assert b2.build_type == "incremental"

        # Archived project rejects POST /api/v1/pulls/ossie with 409
        assert (
            c.post(f"/api/v1/projects/{pid}/archive", headers=headers).status_code
            == 200
        )
        with sessions()() as db:
            before_cnt = db.query(PullRecord).count()
        arch_resp = c.post(
            "/api/v1/pulls/ossie", headers=headers, params={"project": pid}
        )
        assert arch_resp.status_code == 409
        assert "项目已归档" in arch_resp.text
        with sessions()() as db:
            assert db.query(PullRecord).count() == before_cnt
