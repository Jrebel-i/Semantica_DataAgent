"""Catalog-qualified physical tables stay executable without guessing catalogs."""

import json

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
    assert plan["tables"] == [f"{catalog}.shop.orders"]
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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM hive.shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)",
        "SELECT * FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM hive.shop.orders)",
    ],
)
def test_latest_partition_subquery_rejects_mixed_qualification(
    client, monkeypatch, sql
):
    c, headers = _configure(client, monkeypatch, [_metadata()])

    response = c.post("/api/v1/queries/plan", headers=headers, json={"sql": sql})

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


def test_knowledge_summary_preserves_catalog(monkeypatch):
    from semantica_dataagent import knowledge

    row = {
        "table": {
            "name": "orders",
            "database": "shop",
            "ref": "table:shop.orders",
            "catalog": "hive",
            "comment": "",
        },
        "field_count": 1,
        "document_count": 0,
        "has_partition": True,
    }
    monkeypatch.setattr(
        knowledge,
        "_project_settings",
        lambda project_id=None: {"knowledge_generation": "test_generation"},
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(knowledge, "execute", lambda store, query, parameters: [row])

    summary = knowledge.describe(summary=True)

    assert summary[0].get("catalog") == "hive"


def test_table_api_preserves_catalog_without_adding_null(client, monkeypatch):
    from semantica_dataagent import knowledge

    c, headers = client
    summary = {
        "catalog": "hive",
        "name": "orders",
        "database": "shop",
        "comment": "",
        "ref": "table:shop.orders",
        "field_count": 1,
        "document_count": 0,
        "has_partition": True,
    }
    monkeypatch.setattr(knowledge, "describe", lambda **_: [summary])

    response = c.get("/api/v1/tables", params={"view": "summary"}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()[0].get("catalog") == "hive"

    without_catalog = {key: value for key, value in summary.items() if key != "catalog"}
    monkeypatch.setattr(knowledge, "describe", lambda **_: [without_catalog])
    response = c.get("/api/v1/tables", params={"view": "summary"}, headers=headers)
    assert "catalog" not in response.json()[0]


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM TABLE(GENERATE_SERIES(1, 10)) AS "alias_synthetic_marker"',
        'SELECT * FROM missing_table AS "alias_synthetic_marker"',
    ],
)
def test_validate_sql_partition_issue_does_not_echo_aliases(sql):
    from semantica_dataagent.sql_validation import Snapshot, check_sql

    result = check_sql(sql, "doris", Snapshot([], [], []), partition_tables=[])

    assert "synthetic_marker" not in json.dumps(result, ensure_ascii=False)


def _configure_calls(client, monkeypatch, tables):
    from semantica_dataagent import knowledge, query_api

    monkeypatch.setattr(knowledge, "describe", lambda **_: tables)
    calls = []

    class Plugin:
        def bind(self, config, token):
            calls.append("bind")
            return self

        async def estimate(self, sql):
            calls.append("estimate")
            return {"partitions": None, "rows": None}

        async def submit(self, sql, limit):
            calls.append("submit")
            raise AssertionError("rejected plans must never submit")

    monkeypatch.setattr(query_api, "_config", lambda: (Plugin(), {}))
    c, headers = client
    return c, headers, calls


def test_two_part_plan_is_rejected_when_knowledge_records_catalog(client, monkeypatch):
    c, headers, calls = _configure_calls(client, monkeypatch, [_metadata("hive")])

    response = c.post(
        "/api/v1/queries/plan",
        headers=headers,
        json={"sql": "SELECT * FROM shop.orders WHERE dt = '2025-01-01'"},
    )

    assert response.status_code == 422, response.text
    assert "省略了表的目录" in response.text
    assert "hive.shop.orders" not in response.text
    assert calls == []


def test_three_part_plan_succeeds_when_knowledge_records_the_same_catalog(
    client, monkeypatch
):
    c, headers, calls = _configure_calls(client, monkeypatch, [_metadata("hive")])

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
    assert "estimate" in calls


def test_legacy_two_part_plan_still_succeeds_without_catalog_metadata(
    client, monkeypatch
):
    c, headers, calls = _configure_calls(client, monkeypatch, [_metadata()])

    response = c.post(
        "/api/v1/queries/plan",
        headers=headers,
        json={"sql": "SELECT * FROM shop.orders WHERE dt = '2025-01-01'"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["tables"] == ["shop.orders"]
    assert "estimate" in calls


def test_validate_sql_requires_catalog_only_when_knowledge_records_it(monkeypatch):
    from semantica_dataagent.sql_validation import Snapshot, check_sql

    metadata = {**_metadata("hive"), "ref": "table:shop.orders"}
    snapshot = Snapshot([metadata], [], [])

    result = check_sql(
        "SELECT * FROM shop.orders WHERE dt = '2025-01-01'",
        "hive",
        snapshot,
        partition_tables=[metadata],
    )

    assert result["status"] != "pass"
    table_issues = [issue for issue in result["issues"] if issue["kind"] == "table"]
    assert len(table_issues) == 1
    assert table_issues[0]["message"] == "表缺少目录限定: shop.orders"
    assert "hive.shop.orders" in table_issues[0]["suggestion"]


def test_sample_api_requires_catalog_only_when_knowledge_records_it(monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.mcp_contract import ToolInvalidInput
    from semantica_dataagent.sample_api import _table_metadata

    monkeypatch.setattr(knowledge, "describe", lambda **_: [_metadata("hive")])

    try:
        _table_metadata("shop.orders")
    except ToolInvalidInput as error:
        message = str(error)
    else:
        raise AssertionError("unqualified sample must be rejected")
    assert "完整限定" in message
    assert _table_metadata("hive.shop.orders")["catalog"] == "hive"


def _describe_graph(monkeypatch, tables):
    """Fake the graph query shape, filtering by the parameters it receives."""
    from semantica_dataagent import knowledge

    calls = []

    def execute(store, query, parameters):
        calls.append((query, parameters))
        names = parameters.get("names")
        catalog_names = set(parameters.get("catalog_names") or ())
        selected = []
        for table in tables:
            qualified = f"{table['database']}.{table['name']}"
            full_name = (
                f"{table['catalog']}.{qualified}" if table.get("catalog") else ""
            )
            if names is not None and not (
                table["name"] in names
                or qualified in names
                or (full_name and full_name.lower() in catalog_names)
            ):
                continue
            selected.append(table)

        def row(table):
            return {
                **table,
                "ref": table.get("ref") or f"table:{table['database']}.{table['name']}",
            }

        if "count(f) AS field_count" in query:
            return [
                {
                    "table": row(table),
                    "field_count": len(table.get("fields") or ()),
                    "document_count": 0,
                    "has_partition": any(
                        field.get("partition") for field in table.get("fields") or ()
                    ),
                }
                for table in selected
            ]
        return [
            {"table": row(table), "fields": table.get("fields") or [], "documents": []}
            for table in selected
        ]

    monkeypatch.setattr(
        knowledge,
        "_project_settings",
        lambda project_id=None: {"knowledge_generation": "test_generation"},
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(knowledge, "execute", execute)
    return calls


@pytest.mark.parametrize("summary", [False, True])
def test_describe_tables_filters_three_part_names_by_catalog(monkeypatch, summary):
    from semantica_dataagent import knowledge

    calls = _describe_graph(
        monkeypatch,
        [_metadata("hive"), _metadata("internal")],
    )

    rows = knowledge.describe(names=["hive.shop.orders"], summary=summary)

    assert len(rows) == 1
    assert rows[0].get("catalog") == "hive"
    query, parameters = calls[-1]
    assert "t.catalog" in query
    assert "$catalog_names" in query
    assert parameters["catalog_names"] == ["hive.shop.orders"]


@pytest.mark.parametrize("summary", [False, True])
def test_describe_tables_does_not_fall_back_to_another_catalog(monkeypatch, summary):
    from semantica_dataagent import knowledge

    _describe_graph(monkeypatch, [_metadata("internal")])
    assert knowledge.describe(names=["hive.shop.orders"], summary=summary) == []

    _describe_graph(monkeypatch, [_metadata()])
    assert knowledge.describe(names=["hive.shop.orders"], summary=summary) == []
