"""Tests for the Apache Ossie source adapter and derived knowledge documents (#269)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
TPCDS_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "ossie" / "tpcds_semantic_model.yaml"
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as test_client:
        yield test_client, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_ossie_adapter_discovery_defaults_and_project_compatibility(client):
    from semantica_dataagent.plugins import discover

    discovered = discover()
    assert ("source", "ossie") in discovered
    adapter = discovered[("source", "ossie")]
    assert adapter.id == "ossie"
    assert adapter.display_name == "Apache Ossie"
    assert adapter.project_types == ("warehouse",)

    c, admin = client
    default_adapters = c.get(
        "/api/v1/projects/default/adapters", headers=_headers(admin)
    )
    assert default_adapters.status_code == 200, default_adapters.text
    by_id = {row["id"]: row for row in default_adapters.json()}
    assert "ossie" in by_id
    ossie_row = by_id["ossie"]
    assert ossie_row["enabled"] is False
    assert ossie_row["project_types"] == ["warehouse"]
    assert ossie_row["compatible"] is True
    assert ossie_row["reason"] is None
    assert set(ossie_row["parameters_schema"]["properties"]) == {
        "path",
        "category",
        "strip_catalog",
    }

    doc_proj = c.post(
        "/api/v1/projects",
        headers=_headers(admin),
        json={"name": "Docs", "type": "document"},
    )
    assert doc_proj.status_code == 201, doc_proj.text
    doc_id = doc_proj.json()["id"]

    doc_adapters = {
        row["id"]: row
        for row in c.get(
            f"/api/v1/projects/{doc_id}/adapters", headers=_headers(admin)
        ).json()
    }
    assert doc_adapters["ossie"]["compatible"] is False
    assert doc_adapters["ossie"]["enabled"] is False
    assert "数仓" in doc_adapters["ossie"]["reason"]

    rejected = c.put(
        f"/api/v1/projects/{doc_id}/adapters",
        headers=_headers(admin),
        json={"adapters": [{"id": "ossie", "enabled": True, "parameters": {}}]},
    )
    assert rejected.status_code == 422, rejected.text
    assert "数仓" in rejected.text

    enabled = c.put(
        "/api/v1/projects/default/adapters",
        headers=_headers(admin),
        json={
            "adapters": [
                {
                    "id": "ossie",
                    "enabled": True,
                    "parameters": {
                        "path": "semantic",
                        "category": "semantic_docs",
                        "strip_catalog": True,
                    },
                }
            ]
        },
    )
    assert enabled.status_code == 200, enabled.text
    updated = next(row for row in enabled.json() if row["id"] == "ossie")
    assert updated["enabled"] is True
    assert updated["parameters"] == {
        "path": "semantic",
        "category": "semantic_docs",
        "strip_catalog": True,
    }


def _minimal_model(name: str = "sales_model", **extra) -> dict:
    return {
        "version": "0.3.0.dev0",
        "name": name,
        "datasets": [
            {
                "name": "orders",
                "source": "shop.orders",
                "fields": [
                    {
                        "name": "order_id",
                        "expression": {"sql": "order_id"},
                        "datatype": "Integer",
                    }
                ],
            }
        ],
        **extra,
    }


def test_ossie_missing_directory_returns_empty_and_rejects_unsafe_paths(tmp_path: Path):
    from semantica_dataagent.ossie_source import OssieAdapter

    adapter = OssieAdapter()
    empty = adapter.read(tmp_path, {})
    assert empty.records == []
    assert empty.hashes == {}

    (tmp_path / "ossie").mkdir()
    empty_dir = adapter.read(tmp_path, {})
    assert empty_dir.records == []
    assert empty_dir.hashes == {}

    for bad_params in (
        {"path": "../escape"},
        {"path": "/etc"},
        {"path": ".hidden"},
        {"path": "ossie/.hidden"},
        {"category": ".hidden"},
        {"category": "nested/cat"},
        {"category": "../bad"},
    ):
        with pytest.raises(ValueError):
            adapter.read(tmp_path, bad_params)

    outside = tmp_path.parent / "outside.yaml"
    outside.write_text(
        "version: '0.3.0.dev0'\nname: bad\ndatasets: []\n", encoding="utf-8"
    )
    (tmp_path / "ossie" / "link.yaml").symlink_to(outside)
    with pytest.raises(ValueError, match="escape|symlink|Symlink"):
        adapter.read(tmp_path, {})


def test_ossie_rejects_duplicate_json_and_yaml_keys_while_allowing_yaml_merge(
    tmp_path: Path,
):
    from semantica_dataagent.ossie_source import OssieAdapter

    adapter = OssieAdapter()
    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()

    # Duplicate JSON key at nested level
    dup_json = ossie_dir / "dup.json"
    dup_json.write_text(
        '{"version": "0.3.0.dev0", "name": "m", "datasets": [{"name": "a", "name": "b", "source": "s.t"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        adapter.read(tmp_path, {})
    dup_json.unlink()

    # Duplicate YAML key
    dup_yaml = ossie_dir / "dup.yaml"
    dup_yaml.write_text(
        "version: '0.3.0.dev0'\n"
        "name: m1\n"
        "name: m2\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: shop.orders\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        adapter.read(tmp_path, {})
    dup_yaml.unlink()

    # Valid YAML anchor + merge overriding a key should succeed
    merge_yaml = ossie_dir / "merged.yml"
    merge_yaml.write_text(
        "version: '0.3.0.dev0'\n"
        "name: merged_model\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: shop.orders\n"
        "    fields:\n"
        "      - &base_field\n"
        "        name: id_a\n"
        "        expression:\n"
        "          sql: id_a\n"
        "        datatype: String\n"
        "      - <<: *base_field\n"
        "        name: id_b\n"
        "        expression:\n"
        "          sql: id_b\n",
        encoding="utf-8",
    )
    result = adapter.read(tmp_path, {})
    fields = {r.data["name"] for r in result.records if r.kind == "field"}
    assert fields == {"id_a", "id_b"}


def test_ossie_normalizes_legacy_versions_wrappers_and_extensions(tmp_path: Path):
    from semantica_dataagent.ossie_source import OssieAdapter

    adapter = OssieAdapter()
    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()

    # 0.1 wrapped model without version + custom_extensions with JSON-string data
    wrapped = {
        "semantic_model": {
            "name": "legacy_v01",
            "custom_extensions": [
                {"vendor_name": "LEGACY", "data": '{"owner": "data_team"}'}
            ],
            "datasets": [
                {
                    "name": "orders",
                    "source": "shop.orders",
                    "fields": [
                        {
                            "name": "dt",
                            "expression": {"sql": "dt"},
                            "datatype": "String",
                            "custom_extensions": [
                                {
                                    "vendor_name": "SEMANTICA_DATAAGENT",
                                    "data": '{"partition": true, "raw_type": "varchar(10)"}',
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    }
    (ossie_dir / "v01.json").write_text(json.dumps(wrapped), encoding="utf-8")

    # 0.2 model with version 0.2.1 and custom_extensions
    v02 = {
        "version": "0.2.1",
        "name": "legacy_v02",
        "datasets": [
            {
                "name": "customers",
                "source": "shop.customers",
                "custom_extensions": [{"vendor_name": "BI", "data": {"tier": 1}}],
                "fields": [
                    {
                        "name": "cid",
                        "expression": {"sql": "cid"},
                        "datatype": "Integer",
                    }
                ],
            }
        ],
    }
    (ossie_dir / "v02.json").write_text(json.dumps(v02), encoding="utf-8")

    result = adapter.read(tmp_path, {})
    tables = {r.data["name"]: r.data for r in result.records if r.kind == "table"}
    fields = {
        (r.data["table"], r.data["name"]): r.data
        for r in result.records
        if r.kind == "field"
    }
    assert set(tables) == {"orders", "customers"}
    assert fields[("orders", "dt")]["partition"] is True
    assert fields[("orders", "dt")]["type"] == "varchar(10)"
    assert fields[("customers", "cid")]["partition"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"semantic_model": [_minimal_model("a"), _minimal_model("b")]},
        [_minimal_model("a"), _minimal_model("b")],
        {
            "version": "0.2.0.dev0",
            "name": "both_ext",
            "custom_extensions": [{"vendor_name": "A", "data": {}}],
            "nonportable_extensions": [{"vendor_name": "B", "data": {}}],
            "datasets": [{"name": "orders", "source": "shop.orders"}],
        },
        # Flat 0.3.0.dev0 missing required version must fail
        {
            "name": "no_version",
            "datasets": [{"name": "orders", "source": "shop.orders"}],
        },
        # 0.3.0.dev0 with string data in nonportable_extensions must fail schema validation
        _minimal_model(
            "bad_ext_data",
            nonportable_extensions=[{"vendor_name": "X", "data": '{"a": 1}'}],
        ),
        # Invalid version
        {**_minimal_model("bad_ver"), "version": "9.9.9"},
        # Unknown top-level property
        _minimal_model("extra_prop", unexpected_key=True),
    ],
)
def test_ossie_rejects_invalid_wrappers_conflicting_extensions_and_schema_errors(
    tmp_path: Path, payload
):
    from semantica_dataagent.ossie_source import OssieAdapter

    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()
    (ossie_dir / "invalid.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        OssieAdapter().read(tmp_path, {})


@pytest.mark.parametrize(
    "payload",
    [
        _minimal_model(
            "top_custom_ext",
            custom_extensions=[{"vendor_name": "X", "data": {"a": 1}}],
        ),
        {
            "version": "0.3.0.dev0",
            "name": "dataset_custom_ext",
            "datasets": [
                {
                    "name": "orders",
                    "source": "shop.orders",
                    "custom_extensions": [{"vendor_name": "X", "data": {"a": 1}}],
                }
            ],
        },
        {
            "version": "0.3.0.dev0",
            "name": "field_custom_ext",
            "datasets": [
                {
                    "name": "orders",
                    "source": "shop.orders",
                    "fields": [
                        {
                            "name": "order_id",
                            "expression": {"sql": "order_id"},
                            "datatype": "Integer",
                            "custom_extensions": [
                                {
                                    "vendor_name": "SEMANTICA_DATAAGENT",
                                    "data": {"partition": True},
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        _minimal_model(
            "both_ext_03",
            custom_extensions=[{"vendor_name": "A", "data": {}}],
            nonportable_extensions=[{"vendor_name": "B", "data": {}}],
        ),
    ],
)
def test_ossie_rejects_custom_extensions_in_03_document(tmp_path: Path, payload):
    from semantica_dataagent import knowledge
    from semantica_dataagent.ossie_source import OssieAdapter

    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()
    (ossie_dir / "native03.json").write_text(json.dumps(payload), encoding="utf-8")
    adapter = OssieAdapter()
    with pytest.raises(
        ValueError, match=r"Additional properties are not allowed.*custom_extensions"
    ):
        adapter.read(tmp_path, {})
    with pytest.raises(
        ValueError, match=r"Additional properties are not allowed.*custom_extensions"
    ):
        knowledge.read_sources(
            {"source_directory": str(tmp_path), "_project_type": "warehouse"},
            [("source", adapter, True, {})],
        )


def test_ossie_reads_tpcds_official_example_and_generates_derived_docs(tmp_path: Path):
    import shutil

    from semantica_dataagent.knowledge import validate_records
    from semantica_dataagent.ossie_source import OssieAdapter

    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()
    shutil.copy2(TPCDS_FIXTURE, ossie_dir / "tpcds_semantic_model.yaml")

    result = OssieAdapter().read(tmp_path, {})
    validate_records(result.records)

    tables = {
        (r.data.get("catalog"), r.data["database"], r.data["name"]): r.data
        for r in result.records
        if r.kind == "table"
    }
    fields = {
        (r.data["table"], r.data["name"]): r.data
        for r in result.records
        if r.kind == "field"
    }
    rels = [r.data for r in result.records if r.kind == "relationship"]
    docs = {r.data["path"]: r for r in result.records if r.kind == "document"}

    assert set(tables) == {
        ("tpcds", "public", "store_sales"),
        ("tpcds", "public", "date_dim"),
        ("tpcds", "public", "customer"),
        ("tpcds", "public", "item"),
        ("tpcds", "public", "store"),
    }
    # 32 declared fields in tpcds_semantic_model.yaml minus 1 calculated field (customer_full_name)
    assert len(fields) == 31
    assert ("customer", "customer_full_name") not in fields
    assert fields[("date_dim", "d_quarter_name")]["type"] == "string"
    assert fields[("date_dim", "d_moy")]["partition"] is False

    assert len(rels) == 4
    assert all(
        r["from_catalog"] == "tpcds" and r["to_catalog"] == "tpcds" for r in rels
    )

    assert set(docs) == {"tpcds_retail_model.md", "index.md"}
    model_doc = docs["tpcds_retail_model.md"]
    assert model_doc.data["category"] == "ossie"
    assert model_doc.data["generated"] is True
    assert model_doc.source_key in result.hashes
    assert "`tpcds.public.store_sales`" in model_doc.data["content"]
    assert "`tpcds.public.customer`" in model_doc.data["content"]
    assert "customer_full_name" in model_doc.data["content"]
    assert "c_first_name || ' ' || c_last_name" in model_doc.data["content"]
    assert "total_quantity" in model_doc.data["content"]
    assert "customer_lifetime_value" in model_doc.data["content"]
    assert "CLV" in model_doc.data["content"]
    assert "MoM sales" in model_doc.data["content"]

    index_doc = docs["index.md"]
    assert index_doc.data["generated"] is True
    assert index_doc.source_key in result.hashes
    assert index_doc.source_files == ("ossie/tpcds_semantic_model.yaml",)
    assert "tpcds_retail_model.md" in index_doc.data["content"]

    # strip_catalog=True drops the leading catalog from three-part sources
    stripped = OssieAdapter().read(tmp_path, {"strip_catalog": True})
    stripped_tables = [r.data for r in stripped.records if r.kind == "table"]
    assert all("catalog" not in t for t in stripped_tables)
    assert {(t["database"], t["name"]) for t in stripped_tables} == {
        ("public", "store_sales"),
        ("public", "date_dim"),
        ("public", "customer"),
        ("public", "item"),
        ("public", "store"),
    }


@pytest.mark.parametrize(
    "bad_source", ["orders", "a.b.c.d", "a..b", ".a.b", "SELECT * FROM t"]
)
def test_ossie_rejects_invalid_dataset_source(tmp_path: Path, bad_source: str):
    from semantica_dataagent.ossie_source import OssieAdapter

    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()
    payload = {
        "version": "0.3.0.dev0",
        "name": "bad_source_model",
        "datasets": [{"name": "bad", "source": bad_source}],
    }
    (ossie_dir / "bad.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        OssieAdapter().read(tmp_path, {})


def test_ossie_expression_priority_logical_mapping_and_extensions(tmp_path: Path):
    from semantica_dataagent.ossie_source import OssieAdapter

    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()
    model = {
        "version": "0.3.0.dev0",
        "name": "expr_model",
        "datasets": [
            {
                "name": "orders_ds",
                "source": "hive.shop.orders",
                "fields": [
                    # sql overrides OSSIE_SQL_2026 in dialects
                    {
                        "name": "logical_order_id",
                        "label": "订单号",
                        "description": "主键",
                        "datatype": "Integer",
                        "expression": {
                            "sql": "orders_ds.order_id",
                            "dialects": [
                                {
                                    "dialect": "OSSIE_SQL_2026",
                                    "expression": "order_id + 1",
                                }
                            ],
                        },
                        "ai_context": {"synonyms": ["订单编号", "order number"]},
                        "nonportable_extensions": [
                            {
                                "vendor_name": "SEMANTICA_DATAAGENT",
                                "data": {"partition": False, "raw_type": "bigint"},
                            }
                        ],
                    },
                    # OSSIE_SQL_2026 in dialects beats ANSI_SQL even when listed second
                    {
                        "name": "logical_dt",
                        "datatype": "Date",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "COALESCE(dt, '1970-01-01')",
                                },
                                {
                                    "dialect": "OSSIE_SQL_2026",
                                    "expression": "hive.shop.orders.dt",
                                },
                            ]
                        },
                        "nonportable_extensions": [
                            {
                                "vendor_name": "SEMANTICA_DATAAGENT",
                                "data": {"partition": True},
                            }
                        ],
                    },
                    # ANSI_SQL beats dialects[0]
                    {
                        "name": "logical_cust",
                        "datatype": "String",
                        "expression": {
                            "dialects": [
                                {"dialect": "BI_EXPR", "expression": "a + b"},
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "shop.orders.customer_sk",
                                },
                            ]
                        },
                    },
                    # Fallback to dialects[0]
                    {
                        "name": "logical_status",
                        "expression": {
                            "dialects": [
                                {"dialect": "SPARK_SQL", "expression": "`status_code`"}
                            ]
                        },
                    },
                    # Calculated expression & foreign-table qualified column are excluded from physical fields
                    {
                        "name": "calc_amount",
                        "datatype": "Decimal",
                        "expression": {
                            "dialects": [
                                {"dialect": "ANSI_SQL", "expression": "qty * price"},
                                {
                                    "dialect": "SPARK_SQL",
                                    "expression": "ROUND(qty * price, 2)",
                                },
                            ]
                        },
                    },
                    {
                        "name": "foreign_ref",
                        "datatype": "String",
                        "expression": {"sql": "other_table.col_x"},
                    },
                ],
            }
        ],
    }
    (ossie_dir / "expr.json").write_text(json.dumps(model), encoding="utf-8")

    result = OssieAdapter().read(tmp_path, {"category": "semantic_models"})
    fields = {r.data["name"]: r.data for r in result.records if r.kind == "field"}
    assert set(fields) == {"order_id", "dt", "customer_sk", "status_code"}

    assert fields["order_id"]["catalog"] == "hive"
    assert fields["order_id"]["type"] == "bigint"
    assert fields["order_id"]["partition"] is False
    for token in ("订单号", "主键", "logical_order_id", "订单编号", "order number"):
        assert token in fields["order_id"]["comment"]

    assert fields["dt"]["partition"] is True
    assert fields["dt"]["type"] == "date"
    assert fields["status_code"]["type"] == "string"

    doc = next(
        r
        for r in result.records
        if r.kind == "document" and r.data["path"] == "expr_model.md"
    )
    assert doc.data["category"] == "semantic_models"
    assert "calc_amount" in doc.data["content"]
    assert "ROUND(qty * price, 2)" in doc.data["content"]
    assert "foreign_ref" in doc.data["content"]


def test_ossie_relationships_dereference_composite_keys_and_skip_calculated(
    tmp_path: Path,
):
    from semantica_dataagent.ossie_source import OssieAdapter

    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()
    model = {
        "version": "0.3.0.dev0",
        "name": "rel_model",
        "datasets": [
            {
                "name": "sales_fact",
                "source": "shop.orders",
                "fields": [
                    {"name": "logical_tenant", "expression": {"sql": "tenant_id"}},
                    {"name": "logical_cust", "expression": {"sql": "cust_key"}},
                    {
                        "name": "calc_key",
                        "expression": {"sql": "CONCAT(tenant_id, '-', cust_key)"},
                    },
                ],
            },
            {
                "name": "cust_dim",
                "source": "shop.customers",
                "fields": [
                    {"name": "dim_tenant", "expression": {"sql": "tenant_id"}},
                    {"name": "dim_id", "expression": {"sql": "cid"}},
                    {"name": "dim_calc_id", "expression": {"sql": "UPPER(cid)"}},
                ],
            },
        ],
        "relationships": [
            {
                "name": "orders_to_customers",
                "from": "sales_fact",
                "to": "cust_dim",
                "from_columns": ["logical_tenant", "logical_cust"],
                "to_columns": ["dim_tenant", "dim_id"],
            },
            {
                "name": "orders_to_customers_calc",
                "from": "sales_fact",
                "to": "cust_dim",
                "from_columns": ["calc_key"],
                "to_columns": ["dim_calc_id"],
            },
        ],
    }
    (ossie_dir / "rels.json").write_text(json.dumps(model), encoding="utf-8")

    result = OssieAdapter().read(tmp_path, {})
    rels = [r.data for r in result.records if r.kind == "relationship"]
    assert len(rels) == 1
    assert rels[0]["name"] == "orders_to_customers"
    assert rels[0]["from_database"] == "shop"
    assert rels[0]["from_table"] == "orders"
    assert rels[0]["from_columns"] == ["tenant_id", "cust_key"]
    assert rels[0]["to_database"] == "shop"
    assert rels[0]["to_table"] == "customers"
    assert rels[0]["to_columns"] == ["tenant_id", "cid"]
    assert result.stats["skipped_calculated_relationships"] == 1

    doc = next(
        r.data
        for r in result.records
        if r.kind == "document" and r.data["path"] == "rel_model.md"
    )
    assert "orders_to_customers_calc" in doc["content"]

    # Unknown dataset, unknown field, and mismatched column counts fail
    for bad_rel in (
        {
            "name": "bad_ds",
            "from": "missing",
            "to": "cust_dim",
            "from_columns": ["a"],
            "to_columns": ["dim_id"],
        },
        {
            "name": "bad_to_ds",
            "from": "sales_fact",
            "to": "missing",
            "from_columns": ["logical_cust"],
            "to_columns": ["a"],
        },
        {
            "name": "bad_from_col",
            "from": "sales_fact",
            "to": "cust_dim",
            "from_columns": ["unknown"],
            "to_columns": ["dim_id"],
        },
        {
            "name": "bad_to_col",
            "from": "sales_fact",
            "to": "cust_dim",
            "from_columns": ["logical_cust"],
            "to_columns": ["unknown"],
        },
        {
            "name": "bad_len",
            "from": "sales_fact",
            "to": "cust_dim",
            "from_columns": ["logical_tenant", "logical_cust"],
            "to_columns": ["dim_id"],
        },
    ):
        bad_model = {**model, "relationships": [bad_rel]}
        (ossie_dir / "rels.json").write_text(json.dumps(bad_model), encoding="utf-8")
        with pytest.raises(ValueError):
            OssieAdapter().read(tmp_path, {})


def test_ossie_merges_shared_tables_and_rejects_conflicts(tmp_path: Path):
    from semantica_dataagent.ossie_source import OssieAdapter

    ossie_dir = tmp_path / "ossie"
    ossie_dir.mkdir()
    m1 = {
        "version": "0.3.0.dev0",
        "name": "model_one",
        "datasets": [
            {
                "name": "date_dim",
                "source": "tpcds.public.date_dim",
                "description": "模型一日期表",
                "fields": [
                    {
                        "name": "d_date_sk",
                        "expression": {"sql": "d_date_sk"},
                        "datatype": "Integer",
                        "description": "代理键",
                    },
                    {
                        "name": "d_year",
                        "expression": {"sql": "d_year"},
                        "datatype": "Integer",
                    },
                ],
            }
        ],
    }
    m2 = {
        "version": "0.3.0.dev0",
        "name": "model_two",
        "datasets": [
            {
                "name": "calendar_dim",
                "source": "tpcds.public.date_dim",
                "description": "模型二日历表",
                "fields": [
                    {
                        "name": "date_key",
                        "expression": {"sql": "d_date_sk"},
                        "datatype": "Integer",
                        "description": "日期键",
                    },
                    {
                        "name": "d_month",
                        "expression": {"sql": "d_month"},
                        "datatype": "Integer",
                    },
                ],
            }
        ],
    }
    (ossie_dir / "m1.json").write_text(json.dumps(m1), encoding="utf-8")
    (ossie_dir / "m2.json").write_text(json.dumps(m2), encoding="utf-8")

    result = OssieAdapter().read(tmp_path, {})
    tables = [r for r in result.records if r.kind == "table"]
    assert len(tables) == 1
    assert tables[0].source_files == ("ossie/m1.json", "ossie/m2.json")
    assert "模型一日期表" in tables[0].data["comment"]
    assert "模型二日历表" in tables[0].data["comment"]

    fields = {r.data["name"]: r for r in result.records if r.kind == "field"}
    assert set(fields) == {"d_date_sk", "d_year", "d_month"}
    assert fields["d_date_sk"].source_files == ("ossie/m1.json", "ossie/m2.json")
    assert "代理键" in fields["d_date_sk"].data["comment"]
    assert "日期键" in fields["d_date_sk"].data["comment"]
    assert fields["d_year"].source_files == ("ossie/m1.json",)
    assert fields["d_month"].source_files == ("ossie/m2.json",)

    # Conflicting type on shared physical column fails
    m2_bad_type = json.loads(json.dumps(m2))
    m2_bad_type["datasets"][0]["fields"][0]["datatype"] = "String"
    (ossie_dir / "m2.json").write_text(json.dumps(m2_bad_type), encoding="utf-8")
    with pytest.raises(ValueError, match="Conflicting field type"):
        OssieAdapter().read(tmp_path, {})

    # Conflicting partition flag on shared physical column fails
    m2_bad_part = json.loads(json.dumps(m2))
    m2_bad_part["datasets"][0]["fields"][0]["nonportable_extensions"] = [
        {"vendor_name": "SEMANTICA_DATAAGENT", "data": {"partition": True}}
    ]
    (ossie_dir / "m2.json").write_text(json.dumps(m2_bad_part), encoding="utf-8")
    with pytest.raises(ValueError, match="Conflicting partition"):
        OssieAdapter().read(tmp_path, {})

    # Duplicate model.name across files fails
    m2_dup_name = json.loads(json.dumps(m2))
    m2_dup_name["name"] = "model_one"
    (ossie_dir / "m2.json").write_text(json.dumps(m2_dup_name), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate Ossie model name"):
        OssieAdapter().read(tmp_path, {})


def _make_fake_stores():
    class FakeGraph:
        def __init__(self):
            self.nodes: list[dict] = []
            self.rels: list[dict] = []
            self._next_id = 1

        def _find_node(self, node_id: int) -> dict:
            return next(n for n in self.nodes if n["id"] == node_id)

        def _matches_files(self, props: dict, params: dict) -> bool:
            files = set(params.get("files") or ())
            selected_refs = set(params.get("selected_refs") or ())
            return (
                props.get("source_file") in files
                or props.get("source_key") in files
                or bool(files.intersection(props.get("source_files") or ()))
                or props.get("ref") in selected_refs
                or props.get("doc_id") in selected_refs
            )

        def execute_query(self, query: str, params: dict):
            q = " ".join(query.split())
            if q.startswith(("CREATE INDEX", "CREATE FULLTEXT INDEX", "CALL db.await")):
                return {"success": True, "records": []}
            gen = params.get("generation")

            if (
                "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files"
                in q
                and "RETURN n.ref AS ref" in q
            ):
                refs = [
                    {"ref": n["props"]["ref"]}
                    for n in self.nodes
                    if "SDAEntity" in n["labels"]
                    and n["props"].get("generation") == gen
                    and self._matches_files(n["props"], params)
                    and n["props"].get("ref")
                ]
                return {"success": True, "records": refs}

            if (
                "MATCH (:SDATable {generation:$generation})-[r:RELATES_TO]->(:SDATable {generation:$generation}) WHERE r.source_file IN $files"
                in q
            ):
                files = set(params["files"])
                keep, deleted = [], 0
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        s, e = (
                            self._find_node(rel["start"]),
                            self._find_node(rel["end"]),
                        )
                        p = rel["props"]
                        if (
                            s["props"].get("generation") == gen
                            and e["props"].get("generation") == gen
                            and (
                                p.get("source_file") in files
                                or p.get("source_key") in files
                                or bool(files.intersection(p.get("source_files") or ()))
                            )
                        ):
                            deleted += 1
                            continue
                    keep.append(rel)
                self.rels = keep
                return {"success": True, "records": [{"deleted": deleted}]}

            if (
                "MATCH (:SDATable {generation:$generation})-[r:RELATES_TO]->(:SDATable {generation:$generation}) WHERE NOT r.ref IN $refs"
                in q
            ):
                refs = set(params["refs"])
                keep, deleted = [], 0
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        s, e = (
                            self._find_node(rel["start"]),
                            self._find_node(rel["end"]),
                        )
                        if (
                            s["props"].get("generation") == gen
                            and e["props"].get("generation") == gen
                            and rel["props"].get("ref") not in refs
                        ):
                            deleted += 1
                            continue
                    keep.append(rel)
                self.rels = keep
                return {"success": True, "records": [{"deleted": deleted}]}

            if (
                "MATCH (n:SDAEntity {generation:$generation}) WHERE n.source_file IN $files"
                in q
            ):
                victims = {
                    n["id"]
                    for n in self.nodes
                    if "SDAEntity" in n["labels"]
                    and n["props"].get("generation") == gen
                    and self._matches_files(n["props"], params)
                }
                if "DELETE batch_relationship" in q:
                    keep = [
                        r
                        for r in self.rels
                        if r["start"] not in victims and r["end"] not in victims
                    ]
                    deleted = len(self.rels) - len(keep)
                    self.rels = keep
                    return {"success": True, "records": [{"deleted": deleted}]}
                if "DETACH DELETE n" in q:
                    self.rels = [
                        r
                        for r in self.rels
                        if r["start"] not in victims and r["end"] not in victims
                    ]
                    keep = [n for n in self.nodes if n["id"] not in victims]
                    deleted = len(self.nodes) - len(keep)
                    self.nodes = keep
                    return {"success": True, "records": [{"deleted": deleted}]}

            if (
                "MATCH (n:SDAEntity {generation:$generation}) MATCH (n)-[batch_relationship]-()"
                in q
            ):
                victims = {
                    n["id"] for n in self.nodes if n["props"].get("generation") == gen
                }
                keep = [
                    r
                    for r in self.rels
                    if r["start"] not in victims and r["end"] not in victims
                ]
                deleted = len(self.rels) - len(keep)
                self.rels = keep
                return {"success": True, "records": [{"deleted": deleted}]}

            if (
                "MATCH (n:SDAEntity {generation:$generation}) WITH n LIMIT $batch_size DETACH DELETE n"
                in q
            ):
                victims = {
                    n["id"] for n in self.nodes if n["props"].get("generation") == gen
                }
                self.rels = [
                    r
                    for r in self.rels
                    if r["start"] not in victims and r["end"] not in victims
                ]
                keep = [n for n in self.nodes if n["id"] not in victims]
                deleted = len(self.nodes) - len(keep)
                self.nodes = keep
                return {"success": True, "records": [{"deleted": deleted}]}

            if "MATCH (c:SDACategory {generation:$generation})" in q and "DELETE" in q:
                cats = set(params.get("categories") or ())
                if "NOT c.name IN $categories" in q:
                    victims = {
                        n["id"]
                        for n in self.nodes
                        if "SDACategory" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("name") not in cats
                    }
                    if "DELETE batch_relationship" in q:
                        keep = [
                            r
                            for r in self.rels
                            if r["start"] not in victims and r["end"] not in victims
                        ]
                        deleted = len(self.rels) - len(keep)
                        self.rels = keep
                        return {"success": True, "records": [{"deleted": deleted}]}
                    if "DETACH DELETE c" in q:
                        self.rels = [
                            r
                            for r in self.rels
                            if r["start"] not in victims and r["end"] not in victims
                        ]
                        keep = [n for n in self.nodes if n["id"] not in victims]
                        deleted = len(self.nodes) - len(keep)
                        self.nodes = keep
                        return {"success": True, "records": [{"deleted": deleted}]}
                return {"success": True, "records": [{"deleted": 0}]}

            if "RETURN count(*) AS deleted" in q:
                return {"success": True, "records": [{"deleted": 0}]}

            if "CREATE (n:SDAEntity:SDATable) SET n = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDATable"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "CREATE (f:SDAEntity:SDAField) SET f = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDAField"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "CREATE (d:SDAEntity:SDADocument) SET d = row" in q:
                for row in params["rows"]:
                    self.nodes.append(
                        {
                            "id": self._next_id,
                            "labels": {"SDAEntity", "SDADocument"},
                            "props": {**row, "generation": gen},
                        }
                    )
                    self._next_id += 1
                return {"success": True, "records": []}

            if "CREATE (p:SDAEntity:SDAChunk) SET p = row" in q:
                for row in params["rows"]:
                    doc = next(
                        n
                        for n in self.nodes
                        if "SDADocument" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") == row["doc_id"]
                    )
                    chunk = {
                        "id": self._next_id,
                        "labels": {"SDAEntity", "SDAChunk"},
                        "props": {**row, "generation": gen},
                    }
                    self._next_id += 1
                    self.nodes.append(chunk)
                    self.rels.append(
                        {
                            "type": "HAS_CHUNK",
                            "start": doc["id"],
                            "end": chunk["id"],
                            "props": {},
                        }
                    )
                return {"success": True, "records": []}

            if (
                "MERGE (c:SDAEntity:SDACategory {generation:$generation, name:row.name})"
                in q
            ):
                for row in params["rows"]:
                    existing = next(
                        (
                            n
                            for n in self.nodes
                            if "SDACategory" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("name") == row["name"]
                        ),
                        None,
                    )
                    if existing is None:
                        self.nodes.append(
                            {
                                "id": self._next_id,
                                "labels": {"SDAEntity", "SDACategory"},
                                "props": {
                                    "generation": gen,
                                    "build_id": params["build_id"],
                                    **row,
                                },
                            }
                        )
                        self._next_id += 1
                    else:
                        existing["props"].update(
                            {
                                "source_file": row["source_file"],
                                "source_key": row["source_key"],
                                "source_files": row["source_files"],
                                "build_id": params["build_id"],
                            }
                        )
                return {"success": True, "records": []}

            if "RETURN elementId(n) AS id" in q:
                label = (
                    "SDAField"
                    if ":SDAField" in q
                    else "SDADocument"
                    if ":SDADocument" in q
                    else "SDADimensionValue"
                )
                return {
                    "success": True,
                    "records": [
                        {"id": n["id"]}
                        for n in self.nodes
                        if label in n["labels"] and n["props"].get("generation") == gen
                    ],
                }

            if "MERGE (t)-[:HAS_FIELD]->(n)" in q:
                ids = set(params["ids"])
                for f in self.nodes:
                    if f["id"] not in ids:
                        continue
                    for t in self.nodes:
                        if (
                            "SDATable" in t["labels"]
                            and t["props"].get("generation") == gen
                            and t["props"].get("database") == f["props"].get("database")
                            and t["props"].get("name") == f["props"].get("table")
                            and (t["props"].get("catalog") or "")
                            == (f["props"].get("catalog") or "")
                            and not any(
                                r["type"] == "HAS_FIELD"
                                and r["start"] == t["id"]
                                and r["end"] == f["id"]
                                for r in self.rels
                            )
                        ):
                            self.rels.append(
                                {
                                    "type": "HAS_FIELD",
                                    "start": t["id"],
                                    "end": f["id"],
                                    "props": {},
                                }
                            )
                return {"success": True, "records": []}

            if "MERGE (c)-[:HAS_DOCUMENT]->(n)" in q:
                ids = set(params["ids"])
                for d in self.nodes:
                    if d["id"] not in ids:
                        continue
                    for c in self.nodes:
                        if (
                            "SDACategory" in c["labels"]
                            and c["props"].get("generation") == gen
                            and c["props"].get("name") == d["props"].get("category")
                            and not any(
                                r["type"] == "HAS_DOCUMENT"
                                and r["start"] == c["id"]
                                and r["end"] == d["id"]
                                for r in self.rels
                            )
                        ):
                            self.rels.append(
                                {
                                    "type": "HAS_DOCUMENT",
                                    "start": c["id"],
                                    "end": d["id"],
                                    "props": {},
                                }
                            )
                return {"success": True, "records": []}

            if "MERGE (t)-[:HAS_DOCUMENT]->(d)" in q:
                for row in params["rows"]:
                    d = next(
                        (
                            n
                            for n in self.nodes
                            if "SDADocument" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("ref") == row["ref"]
                        ),
                        None,
                    )
                    t = next(
                        (
                            n
                            for n in self.nodes
                            if "SDATable" in n["labels"]
                            and n["props"].get("generation") == gen
                            and n["props"].get("database") == row["database"]
                            and n["props"].get("name") == row["name"]
                            and (n["props"].get("catalog") or "")
                            == (row.get("catalog") or "")
                        ),
                        None,
                    )
                    if (
                        d is not None
                        and t is not None
                        and not any(
                            r["type"] == "HAS_DOCUMENT"
                            and r["start"] == t["id"]
                            and r["end"] == d["id"]
                            for r in self.rels
                        )
                    ):
                        self.rels.append(
                            {
                                "type": "HAS_DOCUMENT",
                                "start": t["id"],
                                "end": d["id"],
                                "props": {},
                            }
                        )
                return {"success": True, "records": []}

            if "MERGE (a)-[r:RELATES_TO {ref: row.props.ref}]->(b)" in q:
                for row in params["rows"]:
                    a = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == row["from_database"]
                        and n["props"].get("name") == row["from_table_name"]
                        and (n["props"].get("catalog") or "") == row["from_catalog"]
                    )
                    b = next(
                        n
                        for n in self.nodes
                        if "SDATable" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("database") == row["to_database"]
                        and n["props"].get("name") == row["to_table_name"]
                        and (n["props"].get("catalog") or "") == row["to_catalog"]
                    )
                    existing = next(
                        (
                            r
                            for r in self.rels
                            if r["type"] == "RELATES_TO"
                            and r["start"] == a["id"]
                            and r["end"] == b["id"]
                            and r["props"].get("ref") == row["props"]["ref"]
                        ),
                        None,
                    )
                    if existing is None:
                        self.rels.append(
                            {
                                "type": "RELATES_TO",
                                "start": a["id"],
                                "end": b["id"],
                                "props": dict(row["props"]),
                            }
                        )
                    else:
                        existing["props"] = dict(row["props"])
                return {"success": True, "records": []}

            if "RETURN DISTINCT n.ref AS ref" in q:
                refs = set(params["refs"])
                return {
                    "success": True,
                    "records": [
                        {"ref": n["props"]["ref"]}
                        for n in self.nodes
                        if "SDAEntity" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") in refs
                    ],
                }

            if (
                "MATCH (n:SDAEntity {generation:$generation, ref:$ref}) RETURN properties(n) AS node"
                in q
            ):
                ref = params["ref"]
                return {
                    "success": True,
                    "records": [
                        {"node": dict(n["props"])}
                        for n in self.nodes
                        if "SDAEntity" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") == ref
                    ],
                }

            if (
                "MATCH (c:SDACategory {generation:$generation}) OPTIONAL MATCH (c)-[:HAS_DOCUMENT]->(d:SDADocument)"
                in q
            ):
                cats = [
                    n
                    for n in self.nodes
                    if "SDACategory" in n["labels"]
                    and n["props"].get("generation") == gen
                ]
                cats.sort(key=lambda c: c["props"]["name"])
                records = []
                for c in cats:
                    docs = [
                        dict(self._find_node(r["end"])["props"])
                        for r in self.rels
                        if r["type"] == "HAS_DOCUMENT" and r["start"] == c["id"]
                    ]
                    records.append(
                        {
                            "name": c["props"]["name"],
                            "ref": c["props"]["ref"],
                            "docs": docs,
                        }
                    )
                return {"success": True, "records": records}

            if (
                "MATCH (d:SDADocument {generation:$generation, ref:$ref}) RETURN properties(d) AS doc"
                in q
            ):
                return {
                    "success": True,
                    "records": [
                        {"doc": dict(n["props"])}
                        for n in self.nodes
                        if "SDADocument" in n["labels"]
                        and n["props"].get("generation") == gen
                        and n["props"].get("ref") == params["ref"]
                    ],
                }

            if "OPTIONAL MATCH (t)-[:HAS_FIELD]->(f:SDAField" in q:
                records = []
                names = params.get("names")
                catalog_names = set(params.get("catalog_names") or ())
                database = params.get("database")
                search = params.get("search")
                for t in self.nodes:
                    if not (
                        "SDATable" in t["labels"]
                        and t["props"].get("generation") == gen
                    ):
                        continue
                    tp = t["props"]
                    full_cat = (
                        f"{tp['catalog']}.{tp['database']}.{tp['name']}".lower()
                        if tp.get("catalog")
                        else ""
                    )
                    if names is not None and not (
                        tp["name"] in names
                        or f"{tp['database']}.{tp['name']}" in names
                        or (full_cat and full_cat in catalog_names)
                    ):
                        continue
                    if database is not None and tp["database"] != database:
                        continue
                    if search is not None and search.lower() not in tp["name"].lower():
                        continue
                    fields_for_t = [
                        dict(self._find_node(r["end"])["props"])
                        for r in self.rels
                        if r["type"] == "HAS_FIELD" and r["start"] == t["id"]
                    ]
                    docs_for_t = [
                        {
                            "ref": self._find_node(r["end"])["props"]["ref"],
                            "path": self._find_node(r["end"])["props"]["path"],
                            "category": self._find_node(r["end"])["props"]["category"],
                        }
                        for r in self.rels
                        if r["type"] == "HAS_DOCUMENT" and r["start"] == t["id"]
                    ]
                    if "count(f) AS field_count" in q:
                        records.append(
                            {
                                "table": dict(tp),
                                "field_count": len(fields_for_t),
                                "document_count": len(docs_for_t),
                                "has_partition": any(
                                    f.get("partition") is True for f in fields_for_t
                                ),
                            }
                        )
                    else:
                        records.append(
                            {
                                "table": dict(tp),
                                "fields": fields_for_t,
                                "documents": docs_for_t,
                            }
                        )
                records.sort(
                    key=lambda r: (
                        r["table"].get("catalog") or "",
                        r["table"]["database"],
                        r["table"]["name"],
                    )
                )
                return {"success": True, "records": records}

            if "-[r:DEPENDS_ON]->(:SDALineageTable" in q:
                return {"success": True, "records": []}

            if "-[r:RELATES_TO]->(:SDATable" in q:
                records = []
                for rel in self.rels:
                    if rel["type"] == "RELATES_TO":
                        s, e = (
                            self._find_node(rel["start"]),
                            self._find_node(rel["end"]),
                        )
                        if (
                            s["props"].get("generation") == gen
                            and e["props"].get("generation") == gen
                        ):
                            records.append({"relationship": dict(rel["props"])})
                return {"success": True, "records": records}

            if "db.index.fulltext.queryNodes" in q:
                scopes = set(params["scopes"])
                category = params.get("category")
                q_lower = params["query"].lower()
                rows = []
                for n in self.nodes:
                    p = n["props"]
                    if p.get("generation") != gen:
                        continue
                    if category is not None and p.get("category") != category:
                        continue
                    in_scope = (
                        ("SDATable" in n["labels"] and "tables" in scopes)
                        or ("SDAField" in n["labels"] and "fields" in scopes)
                        or ("SDAChunk" in n["labels"] and "docs" in scopes)
                    )
                    if not in_scope:
                        continue
                    hay = " ".join(
                        str(p.get(k) or "")
                        for k in ("name", "comment", "description", "content")
                    ).lower()
                    if any(tok and tok in hay for tok in q_lower.split()):
                        rows.append({"source": "fulltext", "item": dict(p)})
                    if (
                        "SDATable" in n["labels"]
                        and "tables" in scopes
                        and category is None
                    ):
                        full_dot = (
                            f"{p.get('database', '')}.{p.get('name', '')}".lower()
                        )
                        cat_dot = (
                            f"{p['catalog']}.{full_dot}".lower()
                            if p.get("catalog")
                            else ""
                        )
                        if q_lower in {p.get("name", "").lower(), full_dot, cat_dot}:
                            rows.append({"source": "exact", "item": dict(p)})
                return {"success": True, "records": rows}

            if "WHERE seed.ref IN $seeds" in q:
                seeds = set(params["seeds"])
                scopes = set(params["scopes"])
                category = params.get("category")
                rows = []
                for seed in self.nodes:
                    if (
                        seed["props"].get("generation") != gen
                        or seed["props"].get("ref") not in seeds
                    ):
                        continue
                    neighbors: list[dict] = []
                    # SDAChunk -> SDADocument -> SDATable
                    if "SDAChunk" in seed["labels"]:
                        for r1 in self.rels:
                            if r1["type"] == "HAS_CHUNK" and r1["end"] == seed["id"]:
                                doc_id = r1["start"]
                                for r2 in self.rels:
                                    if (
                                        r2["type"] == "HAS_DOCUMENT"
                                        and r2["end"] == doc_id
                                    ):
                                        tbl = self._find_node(r2["start"])
                                        if "SDATable" in tbl["labels"]:
                                            neighbors.append(tbl)
                    # SDATable -> SDADocument -> SDAChunk and HAS_FIELD
                    if "SDATable" in seed["labels"]:
                        for r1 in self.rels:
                            if r1["type"] == "HAS_FIELD" and r1["start"] == seed["id"]:
                                neighbors.append(self._find_node(r1["end"]))
                            if (
                                r1["type"] == "HAS_DOCUMENT"
                                and r1["start"] == seed["id"]
                            ):
                                doc_id = r1["end"]
                                for r2 in self.rels:
                                    if (
                                        r2["type"] == "HAS_CHUNK"
                                        and r2["start"] == doc_id
                                    ):
                                        neighbors.append(self._find_node(r2["end"]))
                    for nb in neighbors:
                        np = nb["props"]
                        if np.get("generation") != gen:
                            continue
                        if category is not None and np.get("category") != category:
                            continue
                        in_scope = (
                            ("SDATable" in nb["labels"] and "tables" in scopes)
                            or ("SDAField" in nb["labels"] and "fields" in scopes)
                            or ("SDAChunk" in nb["labels"] and "docs" in scopes)
                        )
                        if in_scope:
                            rows.append(
                                {"seed_ref": seed["props"]["ref"], "item": dict(np)}
                            )
                return {"success": True, "records": rows}

            if "MATCH (c:SDAChunk {generation:$generation})" in q:
                pairs = params.get("pairs") or []
                return {
                    "success": True,
                    "records": [
                        {"ref": n["props"]["ref"], "content": n["props"]["content"]}
                        for n in self.nodes
                        if "SDAChunk" in n["labels"]
                        and n["props"].get("generation") == gen
                        and any(
                            p[0] in n["props"]["content"].lower()
                            and p[1] in n["props"]["content"].lower()
                            for p in pairs
                        )
                    ],
                }

            return {"success": True, "records": []}

        def close(self):
            pass

    class FakeVectors:
        def __init__(self):
            self.items: dict[str, dict] = {}
            self.collection = type("C", (), {"load": lambda self: None})()
            self.client = type("Cl", (), {"disconnect": lambda self: None})()

        def create_collection(self, *args, **kwargs):
            pass

        def get_collection(self, *args, **kwargs):
            pass

        def add_vectors(self, vectors, ids, metadata):
            for vid, meta in zip(ids, metadata, strict=True):
                self.items[vid] = dict(meta)

        def delete_vectors(self, ids):
            for vid in ids:
                self.items.pop(vid, None)

        def search(self, **kwargs):
            flt = kwargs.get("filter") or ""
            hits = []
            for meta in self.items.values():
                scope = meta.get("scope", "")
                if f'"{scope}"' not in flt:
                    continue
                if 'metadata["category"] ==' in flt:
                    cat = meta.get("category") or ""
                    if f'"{cat}"' not in flt:
                        continue
                hits.append({"entity": {"metadata": dict(meta)}})
            return [hits]

    return FakeGraph(), FakeVectors()


def test_ossie_end_to_end_build_search_join_sql_override_and_corrections(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import base64
    from contextlib import contextmanager

    from semantica_dataagent import knowledge, sql_validation
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Build, MetadataOverride, Token, sessions
    from semantica_dataagent.document_api import (
        list_categories,
        read_category_index,
        read_document,
    )
    from semantica_dataagent.mcp_context import bind_mcp_context
    from semantica_dataagent.sql_validation import find_join_path, validate_sql
    from semantica_dataagent.stage_contract import StorageHandles

    sql_validation._snapshot_cache = None
    c, admin = client
    headers = _headers(admin)

    source_dir = tmp_path / "source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={
                "values": {
                    "source_directory": str(source_dir),
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
    assert (
        c.put(
            "/api/v1/projects/default/adapters",
            headers=headers,
            json={"adapters": [{"id": "ossie", "enabled": True, "parameters": {}}]},
        ).status_code
        == 200
    )

    # Upload TPC-DS official sample + a model with composite FK, calculated FK & partition field via source upload API
    composite_model = {
        "version": "0.3.0.dev0",
        "name": "commerce_composite",
        "description": "电商复合键与分区模型",
        "datasets": [
            {
                "name": "orders_fact",
                "source": "tpcds.public.web_orders",
                "description": "网站订单事实表",
                "fields": [
                    {
                        "name": "tenant_logical",
                        "expression": {"sql": "tenant_id"},
                        "datatype": "String",
                    },
                    {
                        "name": "buyer_logical",
                        "expression": {"sql": "buyer_sk"},
                        "datatype": "Integer",
                    },
                    {
                        "name": "order_date",
                        "expression": {"sql": "dt"},
                        "datatype": "Date",
                        "nonportable_extensions": [
                            {
                                "vendor_name": "SEMANTICA_DATAAGENT",
                                "data": {"partition": True, "raw_type": "date"},
                            }
                        ],
                    },
                    {
                        "name": "composite_calc",
                        "expression": {"sql": "CONCAT(tenant_id, '_', buyer_sk)"},
                        "datatype": "String",
                    },
                ],
            },
            {
                "name": "buyers_dim",
                "source": "tpcds.public.buyers",
                "description": "买家维度表",
                "fields": [
                    {
                        "name": "dim_tenant",
                        "expression": {"sql": "tenant_id"},
                        "datatype": "String",
                    },
                    {
                        "name": "dim_buyer_id",
                        "expression": {"sql": "id"},
                        "datatype": "Integer",
                    },
                    {
                        "name": "dim_calc",
                        "expression": {"sql": "UPPER(tenant_id)"},
                        "datatype": "String",
                    },
                ],
            },
        ],
        "relationships": [
            {
                "name": "web_orders_to_buyers",
                "from": "orders_fact",
                "to": "buyers_dim",
                "from_columns": ["tenant_logical", "buyer_logical"],
                "to_columns": ["dim_tenant", "dim_buyer_id"],
            },
            {
                "name": "web_orders_calc_link",
                "from": "orders_fact",
                "to": "buyers_dim",
                "from_columns": ["composite_calc"],
                "to_columns": ["dim_calc"],
            },
        ],
        "metrics": [
            {
                "name": "web_buyer_count",
                "expression": {"sql": "COUNT(DISTINCT orders_fact.buyer_logical)"},
                "description": "网站下单独立买家数",
                "ai_context": {"synonyms": ["下单UV", "web unique buyers"]},
            }
        ],
    }

    upload_resp = c.post(
        "/api/v1/source/upload",
        headers=headers,
        json={
            "target": "ossie",
            "files": [
                {
                    "path": "tpcds_semantic_model.yaml",
                    "content_base64": base64.b64encode(
                        TPCDS_FIXTURE.read_bytes()
                    ).decode(),
                },
                {
                    "path": "commerce.json",
                    "content_base64": base64.b64encode(
                        json.dumps(composite_model).encode()
                    ).decode(),
                },
            ],
        },
    )
    assert upload_resp.status_code == 200, upload_resp.text
    listed_files = c.get("/api/v1/source/files", headers=headers).json()
    assert {"ossie/tpcds_semantic_model.yaml", "ossie/commerce.json"} <= set(
        listed_files
    )

    graph_store, vector_store = _make_fake_stores()

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

    # Approve a MetadataOverride on an Ossie physical field before build
    with sessions().begin() as db:
        db.add(
            MetadataOverride(
                project_id="default",
                target="field:tpcds.public.store_sales.ss_net_profit",
                comment="人工审定净利润口径",
            )
        )

    def run_build(build_type: str) -> tuple[int, str, str | None, dict]:
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with sessions()() as db:
                busy = (
                    db.query(Build)
                    .filter(
                        Build.project_id == "default",
                        Build.status.in_(("queued", "running")),
                    )
                    .first()
                )
            if busy is None:
                break
            time.sleep(0.02)
        with sessions().begin() as db:
            row = Build(
                project_id="default",
                build_type=build_type,
                status="running",
                started_at="2026-11-18T00:00:00Z",
                counts="{}",
            )
            db.add(row)
            db.flush()
            bid = row.id
        knowledge.run_build(bid)
        sql_validation._snapshot_cache = None
        with sessions()() as db:
            row = db.get(Build, bid)
            return row.id, row.status, row.error, json.loads(row.counts)

    _, status, err, counts = run_build("full")
    assert status == "succeeded", err
    assert counts["ossie_skipped_calculated_relationships"] == 1
    assert counts["table"] == 7
    assert counts["relationship"] == 5
    # Generated docs must not count as physical source files
    assert counts["added"] == 2

    # MetadataOverride wins over imported field comment
    described = {t["name"]: t for t in knowledge.describe()}
    assert described["store_sales"]["catalog"] == "tpcds"
    ss_fields = {f["name"]: f for f in described["store_sales"]["fields"]}
    assert ss_fields["ss_net_profit"]["comment"] == "人工审定净利润口径"
    cust_fields = {f["name"]: f for f in described["customer"]["fields"]}
    assert "customer_full_name" not in cust_fields

    # Derived category index & documents
    categories = {cat["name"]: cat for cat in list_categories()}
    assert "ossie" in categories
    assert {doc["path"] for doc in categories["ossie"]["documents"]} == {
        "tpcds_retail_model.md",
        "commerce_composite.md",
    }
    idx = read_category_index("ossie")
    assert idx["generated"] is True
    assert "tpcds_retail_model.md" in idx["content"]
    doc = read_document("document:ossie/tpcds_retail_model.md")
    assert doc["generated"] is True

    # Derived documents are protected from direct correction proposals
    with sessions()() as db:
        admin_id = db.query(Token).first().id
    admin_req = Requester(id=admin_id, role="admin", token=admin)

    def mcp_call(tool_name: str, arguments: dict):
        import asyncio

        from mcp.types import CallToolRequest, CallToolRequestParams

        server = create_mcp()

        async def _run():
            server.get_context = lambda: None
            handler = server._mcp_server.request_handlers[CallToolRequest]
            return (
                await handler(
                    CallToolRequest(
                        method="tools/call",
                        params=CallToolRequestParams(
                            name=tool_name, arguments=arguments
                        ),
                    )
                )
            ).root

        with bind_mcp_context(admin_req):
            return asyncio.run(_run())

    gen_doc_err = mcp_call(
        "propose_correction",
        {
            "target": "document:ossie/tpcds_retail_model.md",
            "patch": "手动修改",
            "reason": "修正",
        },
    )
    assert gen_doc_err.isError
    assert gen_doc_err.structuredContent["code"] == "invalid_input"

    idx_doc_err = mcp_call(
        "propose_correction",
        {
            "target": "document:ossie/index.md",
            "patch": "手动修改",
            "reason": "修正",
        },
    )
    assert idx_doc_err.isError
    assert idx_doc_err.structuredContent["code"] == "invalid_input"

    rest_doc_err = c.post(
        "/api/v1/corrections",
        headers=headers,
        json={
            "target": "document:ossie/tpcds_retail_model.md",
            "patch": "手动修改",
            "reason": "修正",
        },
    )
    assert rest_doc_err.status_code == 422
    assert "editable source" in rest_doc_err.text

    # Submit & approve an override on a physical Ossie field via REST, then re-import
    prop_resp = c.post(
        "/api/v1/corrections",
        headers=headers,
        json={
            "target": "field:tpcds.public.web_orders.buyer_sk",
            "patch": "人工确认买家键",
            "reason": "口径补充",
        },
    )
    assert prop_resp.status_code == 201, prop_resp.text
    approve_resp = c.post(
        f"/api/v1/corrections/{prop_resp.json()['proposal_id']}/approve",
        headers=headers,
    )
    assert approve_resp.status_code == 200, approve_resp.text
    _, status2, err2, _ = run_build("full")
    assert status2 == "succeeded", err2
    web_orders_desc = knowledge.describe(names=["tpcds.public.web_orders"])[0]
    web_fields = {f["name"]: f for f in web_orders_desc["fields"]}
    assert web_fields["buyer_sk"]["comment"] == "人工确认买家键"

    # REST knowledge & category browsing endpoints work on Ossie outputs
    rest_tables = c.get("/api/v1/tables", params={"view": "summary"}, headers=headers)
    assert rest_tables.status_code == 200, rest_tables.text
    assert any(
        row.get("catalog") == "tpcds" and row["name"] == "store_sales"
        for row in rest_tables.json()
    )
    rest_cats = c.get("/api/v1/categories", headers=headers)
    assert rest_cats.status_code == 200, rest_cats.text
    assert any(cat["name"] == "ossie" for cat in rest_cats.json())
    rest_doc = c.get(
        "/api/v1/document",
        params={"doc_id": "document:ossie/tpcds_retail_model.md"},
        headers=headers,
    )
    assert rest_doc.status_code == 200, rest_doc.text
    assert rest_doc.json()["generated"] is True

    # Real MCP tools end-to-end
    mcp_desc = mcp_call("describe_tables", {"names": ["tpcds.public.store_sales"]})
    assert not mcp_desc.isError, mcp_desc.structuredContent
    assert mcp_desc.structuredContent["result"][0]["catalog"] == "tpcds"

    mcp_search_docs = mcp_call(
        "search_knowledge", {"query": "CLV", "scope": "docs", "top_k": 5}
    )
    assert not mcp_search_docs.isError
    assert any(
        item.get("doc_id") == "document:ossie/tpcds_retail_model.md"
        for item in mcp_search_docs.structuredContent["result"]
    )

    mcp_search_tables = mcp_call(
        "search_knowledge", {"query": "下单UV", "scope": "tables", "top_k": 5}
    )
    assert not mcp_search_tables.isError
    assert any(
        item["ref"] == "table:tpcds.public.web_orders"
        for item in mcp_search_tables.structuredContent["result"]
    )

    mcp_join = mcp_call(
        "find_join_path",
        {"tables": ["tpcds.public.store_sales", "tpcds.public.customer"]},
    )
    assert not mcp_join.isError
    assert mcp_join.structuredContent["strength"] == "strong"
    assert mcp_join.structuredContent["steps"][0]["keys"] == [
        "ss_customer_sk=c_customer_sk"
    ]

    mcp_sql = mcp_call(
        "validate_sql",
        {
            "sql": (
                "SELECT s.ss_ticket_number, c.c_first_name "
                "FROM tpcds.public.store_sales s "
                "JOIN tpcds.public.customer c ON s.ss_customer_sk = c.c_customer_sk"
            ),
            "dialect": "hive",
        },
    )
    assert not mcp_sql.isError
    assert mcp_sql.structuredContent["status"] == "pass"

    # Direct Python API checks for partial composite FK, missing partition, and computed field rejection
    doc_hits = knowledge.search("CLV", "docs", 5)
    assert any(
        h.get("doc_id") == "document:ossie/tpcds_retail_model.md" for h in doc_hits
    )
    table_hits = knowledge.search("下单UV", "tables", 5)
    assert any(h["ref"] == "table:tpcds.public.web_orders" for h in table_hits)

    path = find_join_path(["tpcds.public.store_sales", "tpcds.public.customer"])
    assert path["status"] == "found"
    assert path["strength"] == "strong"
    assert path["steps"][0]["keys"] == ["ss_customer_sk=c_customer_sk"]
    assert path["steps"][0]["evidence"][0]["type"] == "relationship"

    ok_comp = validate_sql(
        "SELECT o.buyer_sk FROM tpcds.public.web_orders o "
        "JOIN tpcds.public.buyers b ON o.tenant_id = b.tenant_id AND o.buyer_sk = b.id "
        "WHERE o.dt = '2026-01-01'",
        "hive",
    )
    assert ok_comp["status"] == "pass", ok_comp

    # Partial composite FK is rejected
    partial_comp = validate_sql(
        "SELECT o.buyer_sk FROM tpcds.public.web_orders o "
        "JOIN tpcds.public.buyers b ON o.tenant_id = b.tenant_id "
        "WHERE o.dt = '2026-01-01'",
        "hive",
    )
    assert partial_comp["status"] == "issues"
    assert partial_comp["joins"][0]["status"] != "supported"

    # Missing partition filter on web_orders is rejected (partition flag recovered from extension)
    missing_part = validate_sql(
        "SELECT o.buyer_sk FROM tpcds.public.web_orders o",
        "hive",
    )
    assert missing_part["status"] == "issues"
    assert any(issue["kind"] == "partition" for issue in missing_part["issues"])

    # Calculated field is not present on SDAField, so SQL referencing it fails field check
    calc_field_sql = validate_sql(
        "SELECT c.customer_full_name FROM tpcds.public.customer c",
        "hive",
    )
    assert calc_field_sql["status"] == "issues"
    assert any(issue["kind"] == "field" for issue in calc_field_sql["issues"])


def test_ossie_incremental_build_with_shared_table_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    from semantica_dataagent import knowledge, sql_validation
    from semantica_dataagent.cli import initialize
    from semantica_dataagent.db import Build, GenerationPointer, sessions
    from semantica_dataagent.document_api import read_category_index
    from semantica_dataagent.sql_validation import validate_sql
    from semantica_dataagent.stage_contract import StorageHandles

    sql_validation._snapshot_cache = None
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()

    source = tmp_path / "source"
    ossie_dir = source / "ossie"
    ossie_dir.mkdir(parents=True)

    m1 = {
        "version": "0.3.0.dev0",
        "name": "shared_a",
        "datasets": [
            {
                "name": "date_dim",
                "source": "tpcds.public.date_dim",
                "description": "模型A日期说明",
                "fields": [
                    {
                        "name": "d_date_sk",
                        "expression": {"sql": "d_date_sk"},
                        "datatype": "Integer",
                        "description": "A键",
                    },
                    {
                        "name": "d_date",
                        "expression": {"sql": "d_date"},
                        "datatype": "Date",
                    },
                ],
            }
        ],
    }
    m2 = {
        "version": "0.3.0.dev0",
        "name": "shared_b",
        "datasets": [
            {
                "name": "calendar_dim",
                "source": "tpcds.public.date_dim",
                "description": "模型B日期说明",
                "fields": [
                    {
                        "name": "d_date_sk",
                        "expression": {"sql": "d_date_sk"},
                        "datatype": "Integer",
                        "description": "B键",
                    },
                    {
                        "name": "d_year",
                        "expression": {"sql": "d_year"},
                        "datatype": "Integer",
                    },
                ],
            }
        ],
    }
    (ossie_dir / "m1.json").write_text(json.dumps(m1), encoding="utf-8")
    (ossie_dir / "m2.json").write_text(json.dumps(m2), encoding="utf-8")

    graph_store, vector_store = _make_fake_stores()

    @contextmanager
    def fake_storage(values, stats=None):
        yield StorageHandles(
            values["knowledge_generation"],
            graph_store,
            vector_store,
            values,
            _embedding_dimension=4,
        )

    base_settings = {
        "source_directory": str(source),
        "knowledge_generation": "gen_init",
        "neo4j_uri": "bolt://127.0.0.1:7687",
        "neo4j_user": "neo4j",
        "neo4j_password": "secret",
        "milvus_uri": "http://127.0.0.1:19530",
        "embedding_url": "http://127.0.0.1:8000",
        "embedding_model": "test-model",
    }
    monkeypatch.setattr(
        knowledge, "settings", lambda project_id=None: dict(base_settings)
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

    from semantica_dataagent.ossie_source import OssieAdapter

    monkeypatch.setattr(
        knowledge,
        "configured",
        lambda project_id=None, project_type=None: [
            ("source", OssieAdapter(), True, {})
        ],
    )

    def run_build(build_type: str) -> tuple[str, str | None, dict]:
        with sessions().begin() as db:
            row = Build(
                project_id="default",
                build_type=build_type,
                status="running",
                started_at="2026-11-18T00:00:00Z",
                counts="{}",
            )
            db.add(row)
            db.flush()
            bid = row.id
        knowledge.run_build(bid)
        sql_validation._snapshot_cache = None
        with sessions()() as db:
            row = db.get(Build, bid)
            pointer = db.get(GenerationPointer, 1)
            if pointer:
                base_settings["knowledge_generation"] = pointer.generation
            return row.status, row.error, json.loads(row.counts)

    status, err, counts = run_build("full")
    assert status == "succeeded", err
    assert counts["added"] == 2
    date_nodes = [
        n
        for n in graph_store.nodes
        if "SDATable" in n["labels"] and n["props"]["name"] == "date_dim"
    ]
    assert len(date_nodes) == 1
    assert date_nodes[0]["props"]["source_files"] == ["ossie/m1.json", "ossie/m2.json"]

    # 1. Modify second shared model: swap d_year -> d_month
    m2_v2 = json.loads(json.dumps(m2))
    m2_v2["datasets"][0]["description"] = "模型B更新说明"
    m2_v2["datasets"][0]["fields"][1] = {
        "name": "d_month",
        "expression": {"sql": "d_month"},
        "datatype": "Integer",
    }
    (ossie_dir / "m2.json").write_text(json.dumps(m2_v2), encoding="utf-8")
    status, err, counts = run_build("incremental")
    assert status == "succeeded", err
    assert counts["changed"] == 1
    described = knowledge.describe(names=["tpcds.public.date_dim"])
    assert len(described) == 1
    assert "模型B更新说明" in described[0]["comment"]
    field_names = {f["name"] for f in described[0]["fields"]}
    assert field_names == {"d_date_sk", "d_date", "d_month"}
    assert "field:tpcds.public.date_dim.d_year" not in vector_store.items

    # 2. Delete second shared model: date_dim and shared d_date_sk MUST survive from m1.json!
    (ossie_dir / "m2.json").unlink()
    status, err, counts = run_build("incremental")
    assert status == "succeeded", err
    assert counts["deleted"] == 1
    date_nodes_after_del = [
        n
        for n in graph_store.nodes
        if "SDATable" in n["labels"] and n["props"]["name"] == "date_dim"
    ]
    assert len(date_nodes_after_del) == 1
    assert date_nodes_after_del[0]["props"]["source_files"] == ["ossie/m1.json"]
    described_after_del = knowledge.describe(names=["tpcds.public.date_dim"])
    assert len(described_after_del) == 1
    assert "模型B更新说明" not in described_after_del[0]["comment"]
    assert {f["name"] for f in described_after_del[0]["fields"]} == {
        "d_date_sk",
        "d_date",
    }
    assert "field:tpcds.public.date_dim.d_month" not in vector_store.items
    assert "document:ossie/shared_b.md" not in {
        n["props"]["ref"] for n in graph_store.nodes if "SDADocument" in n["labels"]
    }
    assert "shared_b.md" not in read_category_index("ossie")["content"]
    assert (
        validate_sql("SELECT d_date_sk, d_date FROM tpcds.public.date_dim", "hive")[
            "status"
        ]
        == "pass"
    )

    # 3. Add a third shared model m3.json while m1.json is untouched: no duplicate SDATable/SDAField nodes!
    m3 = {
        "version": "0.3.0.dev0",
        "name": "shared_c",
        "datasets": [
            {
                "name": "date_c",
                "source": "tpcds.public.date_dim",
                "description": "模型C日期说明",
                "fields": [
                    {
                        "name": "d_date_sk",
                        "expression": {"sql": "d_date_sk"},
                        "datatype": "Integer",
                    },
                    {
                        "name": "d_week",
                        "expression": {"sql": "d_week"},
                        "datatype": "Integer",
                    },
                ],
            }
        ],
    }
    (ossie_dir / "m3.json").write_text(json.dumps(m3), encoding="utf-8")
    status, err, counts = run_build("incremental")
    assert status == "succeeded", err
    assert counts["added"] == 1
    date_nodes_after_add = [
        n
        for n in graph_store.nodes
        if "SDATable" in n["labels"] and n["props"]["name"] == "date_dim"
    ]
    assert len(date_nodes_after_add) == 1
    assert date_nodes_after_add[0]["props"]["source_files"] == [
        "ossie/m1.json",
        "ossie/m3.json",
    ]
    sk_nodes = [
        n
        for n in graph_store.nodes
        if "SDAField" in n["labels"] and n["props"]["name"] == "d_date_sk"
    ]
    assert len(sk_nodes) == 1
    described_after_add = knowledge.describe(names=["tpcds.public.date_dim"])
    assert {f["name"] for f in described_after_add[0]["fields"]} == {
        "d_date_sk",
        "d_date",
        "d_week",
    }


def test_ossie_coexists_with_canonical_and_rejects_category_collision_and_doc_project(
    tmp_path: Path,
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.canonical import CanonicalAdapter
    from semantica_dataagent.markdown import MarkdownAdapter
    from semantica_dataagent.ossie_source import OssieAdapter

    source = tmp_path / "source"
    (source / "ossie").mkdir(parents=True)
    (source / "knowledge" / "rules").mkdir(parents=True)

    (source / "tables.json").write_text(
        json.dumps([{"database": "ods", "name": "raw_events", "comment": "原始日志"}]),
        encoding="utf-8",
    )
    (source / "ossie" / "orders.json").write_text(
        json.dumps(_minimal_model("orders_model")), encoding="utf-8"
    )
    (source / "knowledge" / "rules" / "biz.md").write_text(
        "# 规则\n\n口径", encoding="utf-8"
    )

    plugins = [
        ("source", CanonicalAdapter(), True, {}),
        ("source", MarkdownAdapter(), True, {}),
        ("source", OssieAdapter(), True, {}),
    ]
    values = {"source_directory": str(source), "_project_type": "warehouse"}
    records, _, _, _ = knowledge.read_sources(values, plugins)
    knowledge.validate_records(records)
    assert {r.data["name"] for r in records if r.kind == "table"} == {
        "raw_events",
        "orders",
    }

    # Document project never calls OssieAdapter even if marked enabled
    doc_values = {"source_directory": str(source), "_project_type": "document"}
    doc_records, doc_hashes, _, _ = knowledge.read_sources(doc_values, plugins)
    assert not any(r.kind == "table" for r in doc_records)
    assert not any(path.startswith("ossie/") for path in doc_hashes)

    # Same category collision between markdown and ossie is caught by validate_records
    (source / "knowledge" / "ossie").mkdir(parents=True)
    (source / "knowledge" / "ossie" / "extra.md").write_text("# 冲突", encoding="utf-8")
    colliding_records, _, _, _ = knowledge.read_sources(values, plugins)
    with pytest.raises(ValueError, match="Duplicate document"):
        knowledge.validate_records(colliding_records)
    (source / "knowledge" / "ossie" / "extra.md").unlink()
    (source / "knowledge" / "ossie").rmdir()

    # Unknown JSON outside ossie owned directory is still rejected by CanonicalAdapter
    (source / "stray.json").write_text('[{"x": 1}]', encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown entity file"):
        knowledge.read_sources(values, plugins)
