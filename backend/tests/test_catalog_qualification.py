"""Catalog-qualified physical tables stay executable without guessing catalogs."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def _metadata(catalog=None, database="shop", name="orders"):
    item = {
        "database": database,
        "name": name,
        "fields": [{"name": "dt", "partition": True}],
    }
    if catalog is not None:
        item["catalog"] = catalog
    return item


def _configure(client, monkeypatch, tables):
    from semantica_dataagent import knowledge, query_api

    monkeypatch.setattr(knowledge, "describe", lambda **_: tables)

    class Plugin:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

    monkeypatch.setattr(query_api, "_config", lambda: (Plugin(), {}))
    return client


@pytest.mark.parametrize("catalog", ["hive", "internal", "AwsDataCatalog"])
def test_catalog_qualified_snapshot_query_preserves_catalog(
    client, monkeypatch, catalog
):
    c, headers = _configure(client, monkeypatch, [_metadata()])
    sql = (
        f"SELECT * FROM {catalog}.shop.orders "
        f"WHERE dt = (SELECT MAX(dt) FROM {catalog}.shop.orders)"
    )

    response = c.post("/api/v1/queries/plan", headers=headers, json={"sql": sql})

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["tables"] == [f"{catalog.lower()}.shop.orders"]
    assert plan["partition_selection"] == "latest_at_runtime"


def test_catalog_aware_metadata_selects_exact_catalog(client, monkeypatch):
    c, headers = _configure(
        client,
        monkeypatch,
        [_metadata("internal"), _metadata("hive")],
    )

    response = c.post(
        "/api/v1/queries/plan",
        headers=headers,
        json={
            "sql": (
                "SELECT * FROM hive.shop.orders "
                "WHERE dt = (SELECT MAX(dt) FROM hive.shop.orders)"
            )
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["tables"] == ["hive.shop.orders"]


def test_two_part_name_is_rejected_when_multiple_catalogs_have_it(client, monkeypatch):
    c, headers = _configure(
        client,
        monkeypatch,
        [_metadata("internal"), _metadata("hive")],
    )

    response = c.post(
        "/api/v1/queries/plan",
        headers=headers,
        json={"sql": "SELECT * FROM shop.orders WHERE dt = '2025-01-01'"},
    )

    assert response.status_code == 422, response.text
    assert "无法确定表在哪个目录下" in response.text


def test_unknown_catalog_without_metadata_is_not_guessed(client, monkeypatch):
    c, headers = _configure(client, monkeypatch, [_metadata()])

    response = c.post(
        "/api/v1/queries/plan",
        headers=headers,
        json={"sql": "SELECT * FROM mysql.shop.orders WHERE dt = '2025-01-01'"},
    )

    assert response.status_code == 422, response.text
    assert "无法确认表的目录元数据" in response.text


def test_latest_partition_subquery_must_use_the_same_catalog(client, monkeypatch):
    c, headers = _configure(client, monkeypatch, [_metadata()])

    response = c.post(
        "/api/v1/queries/plan",
        headers=headers,
        json={
            "sql": (
                "SELECT * FROM hive.shop.orders "
                "WHERE dt = (SELECT MAX(dt) FROM internal.shop.orders)"
            )
        },
    )

    assert response.status_code == 422, response.text
    assert "缺少分区条件" in response.text


def test_validate_sql_reports_and_keeps_catalog_qualification(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.sql_validation import Snapshot, check_sql

    metadata = _metadata()
    snapshot = Snapshot([{**metadata, "ref": "table:shop.orders"}], [], [])
    monkeypatch.setattr(knowledge, "describe", lambda **_: [metadata])

    result = check_sql(
        "SELECT * FROM hive.shop.orders "
        "WHERE dt = (SELECT MAX(dt) FROM hive.shop.orders)",
        "hive",
        snapshot,
        partition_tables=[metadata],
    )

    assert result["status"] == "pass", result
    assert result["tables"][0]["name"] == "hive.shop.orders"


def test_sample_sql_keeps_requested_catalog(monkeypatch):
    from semantica_dataagent import knowledge, sample_api

    metadata = _metadata()
    monkeypatch.setattr(knowledge, "describe", lambda **_: [metadata])

    resolved = sample_api._table_metadata("hive.shop.orders")
    sql = sample_api._build_sql(
        resolved,
        ["id"],
        [{"field": "dt", "value": "2025-01-03"}],
        2,
    )

    assert resolved["catalog"] == "hive"
    assert "`hive`.`shop`.`orders`" in sql
    assert "`dt` = '2025-01-03'" in sql
