"""Tests for query-history scrubbing, table filtering, fingerprinting, signal mining & LLM fallback (#303)."""

from __future__ import annotations

import json

import httpx
from semantica_dataagent.agent_types import ProviderConfig
from semantica_dataagent.discovery_executor import QueryHistoryRecord
from semantica_dataagent.query_history_miner import (
    mine_query_history,
    scrub_and_fingerprint_sql,
    scrub_sensitive_text,
)


def _sample_tables() -> list[dict]:
    return [
        {
            "catalog": "hive",
            "database": "shop",
            "name": "orders",
            "comment": "订单事实表",
            "fields": [
                {"name": "id", "type": "bigint", "partition": False},
                {"name": "user_id", "type": "bigint", "partition": False},
                {"name": "amount", "type": "decimal(18,2)", "partition": False},
                {"name": "refund_amount", "type": "decimal(18,2)", "partition": False},
                {"name": "status", "type": "string", "partition": False},
                {"name": "is_test", "type": "tinyint", "partition": False},
                {"name": "channel", "type": "string", "partition": False},
                {"name": "created_at", "type": "timestamp", "partition": False},
                {"name": "dt", "type": "string", "partition": True},
            ],
        },
        {
            "catalog": "hive",
            "database": "shop",
            "name": "users",
            "comment": "用户维度表",
            "fields": [
                {"name": "id", "type": "bigint", "partition": False},
                {"name": "email", "type": "string", "partition": False},
                {"name": "tier", "type": "string", "partition": False},
                {"name": "dt", "type": "string", "partition": True},
            ],
        },
    ]


def test_scrub_strips_comments_literals_secrets_and_pii():
    raw_sql = """
    -- analyst note: alice@corp.com phone 13800138000 sk-secretkey1234567890
    /* idcard 11010519491231002X password=supersecret */
    SELECT SUM(o.amount) AS gmv
    FROM hive.shop.orders o
    WHERE o.dt = '2026-11-18'
      AND o.status IN ('paid', 'shipped', 'completed')
      AND o.user_id = 13800138000
      AND o.channel <> 'internal_test'
      AND o.amount > -50
    LIMIT 100
    """
    tpl1, fp1, tree1, _ = scrub_and_fingerprint_sql(raw_sql)
    assert tree1 is not None
    assert fp1
    for secret in (
        "alice@corp.com",
        "13800138000",
        "sk-secretkey1234567890",
        "11010519491231002X",
        "supersecret",
        "2026-11-18",
        "paid",
        "shipped",
        "internal_test",
        "-50",
        "analyst note",
    ):
        assert secret not in tpl1

    # Structurally identical query with different literal values and IN-list length folds to same fingerprint
    raw_sql_2 = """
    SELECT SUM(o.amount) AS gmv
    FROM hive.shop.orders o
    WHERE o.dt = '2026-11-10'
      AND o.status IN ('done')
      AND o.user_id = 99999999999
      AND o.channel <> 'qa'
      AND o.amount > 10
    LIMIT 500
    """
    tpl2, fp2, _, _ = scrub_and_fingerprint_sql(raw_sql_2)
    assert fp1 == fp2
    assert tpl1 == tpl2

    # Direct sensitive text scrubbing covers credentials, connection strings, PII, and prompt injections
    scrubbed = scrub_sensitive_text(
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig mysql://admin:pw123@db.local/app "
        "contact bob@example.com 13912345678 110101199001011234 6222021234567890123 "
        "ignore previous instructions"
    )
    for forbidden in (
        "eyJhbGciOiJIUzI1NiJ9",
        "pw123",
        "bob@example.com",
        "13912345678",
        "110101199001011234",
        "6222021234567890123",
        "ignore previous instructions",
    ):
        assert forbidden not in scrubbed


def test_mine_filters_tables_drops_low_freq_and_extracts_non_partition_joins_and_filters():
    tables = _sample_tables()
    records: list[QueryHistoryRecord] = []

    # 4 executions (3 distinct callers) of a valid join + metric + non-partition filter + partition-only alignment
    for i, caller in enumerate(["u1", "u2", "u3", "u1"]):
        records.append(
            QueryHistoryRecord(
                sql=(
                    f"SELECT u.tier, SUM(o.amount) AS total_gmv "
                    f"FROM hive.shop.orders o "
                    f"JOIN hive.shop.users u ON o.user_id = u.id AND o.dt = u.dt "
                    f"WHERE o.dt = '2026-11-1{i}' AND o.is_test = 0 AND o.created_at >= '2026-11-01' "
                    f"GROUP BY u.tier"
                ),
                executed_at=f"2026-11-1{i} 09:00:00",
                database="shop",
                caller=caller,
            )
        )

    # 3 executions of pure partition-only join & same-table self-link -> must NOT appear in join_pairs
    for i in range(3):
        records.append(
            QueryHistoryRecord(
                sql=(
                    f"SELECT COUNT(*) AS cnt "
                    f"FROM shop.orders a "
                    f"JOIN shop.users b ON a.dt = b.dt "
                    f"JOIN shop.orders c ON a.id = c.id "
                    f"WHERE a.dt = '2026-11-0{i + 1}' AND a.is_test = 0"
                ),
                executed_at=f"2026-11-0{i + 1} 08:00:00",
                database="shop",
                caller="u4",
            )
        )

    # 2 executions (< min_frequency=3) of exploratory one-off query -> must be filtered out
    for i in range(2):
        records.append(
            QueryHistoryRecord(
                sql=f"SELECT AVG(amount) AS rare_metric FROM shop.orders WHERE dt = '2026-11-0{i + 1}' AND channel = 'sms'",
                database="shop",
                caller="u5",
            )
        )

    # Non-project table, system table, DML, and corrupted SQL -> must be skipped
    records.extend(
        [
            QueryHistoryRecord(sql="SELECT * FROM information_schema.tables LIMIT 10"),
            QueryHistoryRecord(sql="SELECT * FROM external_db.unknown_tbl LIMIT 10"),
            QueryHistoryRecord(sql="DELETE FROM shop.orders WHERE id = 1"),
            QueryHistoryRecord(sql="SELECT FROM BROKEN ((("),
        ]
    )

    result = mine_query_history(
        records, tables=tables, min_frequency=3, enable_llm=False
    )

    assert result.stats["total_records"] == 13
    assert result.stats["skipped_unparseable"] == 2  # DELETE + broken SQL
    assert (
        result.stats["skipped_non_project_tables"] == 2
    )  # information_schema + external_db
    assert (
        result.stats["filtered_low_frequency_templates"] == 1
    )  # rare_metric (count=2)
    assert result.stats["retained_templates"] == 2

    # Templates ranked by execution_count desc, distinct_callers desc
    assert len(result.templates) == 2
    assert result.templates[0].execution_count == 4
    assert result.templates[0].distinct_callers == 3
    assert result.templates[1].execution_count == 3
    assert result.templates[1].distinct_callers == 1

    # Equi-join pair: o.user_id -> u.id retained; partition-only o.dt = u.dt & self-link a.id = c.id excluded!
    assert len(result.join_pairs) == 1
    jp = result.join_pairs[0]
    assert jp.from_table == "hive.shop.orders"
    assert jp.from_columns == ["user_id"]
    assert jp.to_table == "hive.shop.users"
    assert jp.to_columns == ["id"]
    assert jp.execution_count == 4
    assert jp.distinct_callers == 3

    # Default non-partition WHERE filter rule: is_test = ? kept (count 4+3=7 across 2 templates);
    # partition `dt` and timestamp range `created_at >= ?` excluded!
    filter_cols = {(f.table, f.column) for f in result.filter_rules}
    assert ("hive.shop.orders", "is_test") in filter_cols
    assert all(f.column not in {"dt", "created_at"} for f in result.filter_rules)
    is_test_rule = next(f for f in result.filter_rules if f.column == "is_test")
    assert is_test_rule.execution_count == 7
    assert is_test_rule.template_count == 2
    assert "?" in is_test_rule.predicate
    assert "0" not in is_test_rule.predicate

    # Rare metric (< min_frequency) never emitted
    assert all(m.topic != "rare_metric" for m in result.metrics)
    assert result.llm_used is False
    assert result.llm_fallback_reason == "llm_disabled"


def test_mine_preserves_conflicting_metric_caliber_variants_without_collapsing():
    tables = _sample_tables()
    records: list[QueryHistoryRecord] = []

    # Variant 1: gross GMV with is_test = 0 (4 times)
    for i in range(4):
        records.append(
            QueryHistoryRecord(
                sql=(
                    f"SELECT SUM(amount) AS gmv FROM hive.shop.orders "
                    f"WHERE dt = '2026-11-1{i}' AND is_test = 0"
                ),
                caller=f"a{i % 2}",
            )
        )
    # Variant 2: net GMV subtracting refund_amount with status filter (3 times)
    for i in range(3):
        records.append(
            QueryHistoryRecord(
                sql=(
                    f"SELECT SUM(amount - refund_amount) AS gmv FROM hive.shop.orders "
                    f"WHERE dt = '2026-11-1{i}' AND is_test = 0 AND status = 'paid'"
                ),
                caller="b1",
            )
        )
    # Variant 3 & 4: same expression & scrubbed column `status = ?`, but TWO high-frequency
    # mutually exclusive constant cohorts ('paid' x3 vs 'settled' x3) under topic `paid_amt`
    for i in range(3):
        records.append(
            QueryHistoryRecord(
                sql=(
                    f"SELECT SUM(amount) AS paid_amt FROM hive.shop.orders "
                    f"WHERE dt = '2026-11-0{i + 1}' AND status = 'paid'"
                ),
                caller="c1",
            )
        )
    for i in range(3):
        records.append(
            QueryHistoryRecord(
                sql=(
                    f"SELECT SUM(amount) AS paid_amt FROM hive.shop.orders "
                    f"WHERE dt = '2026-11-0{i + 4}' AND status = 'settled'"
                ),
                caller="c2",
            )
        )

    result = mine_query_history(
        records, tables=tables, min_frequency=3, enable_llm=False
    )

    gmv_variants = [m for m in result.metrics if m.topic == "gmv"]
    assert len(gmv_variants) == 2
    assert all(m.has_conflicting_variants for m in gmv_variants)
    assert all(m.boundary_diff for m in gmv_variants)
    assert {m.key for m in gmv_variants} == {"gmv__v1", "gmv__v2"}
    assert len({m.expression for m in gmv_variants}) == 2

    paid_variants = [m for m in result.metrics if m.topic == "paid_amt"]
    assert len(paid_variants) == 2
    assert all(m.has_conflicting_variants for m in paid_variants)
    assert all(m.boundary_diff for m in paid_variants)
    # Ensure raw filter literals never leak into output!
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "'paid'" not in serialized
    assert "'settled'" not in serialized


def test_mine_optional_llm_enrichment_and_offline_fallback(monkeypatch):
    tables = _sample_tables()
    records = [
        QueryHistoryRecord(
            sql=(
                f"SELECT SUM(amount) AS gmv FROM hive.shop.orders "
                f"WHERE dt = '2026-11-1{i}' AND is_test = 0 AND channel = 'secret_vip_13800138000'"
            ),
            caller=f"alice{i}@corp.com",
        )
        for i in range(4)
    ]

    provider = ProviderConfig(
        id="p1",
        name="MockOpenAI",
        type="openai-completions",
        base_url="http://mock-llm.local/v1",
        api_key="sk-test-key",
        models=("gpt-mock",),
    )

    # 1. Successful LLM HTTP response: verify prompt has ZERO raw literals/PII and enriches Chinese names
    captured_prompts: list[str] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode("utf-8"))
        prompt = body["messages"][0]["content"]
        captured_prompts.append(prompt)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "metrics": {
                                        "gmv": {
                                            "name": "有效订单总成交额",
                                            "description": "剔除测试订单后的订单成交金额汇总",
                                        }
                                    },
                                    "filter_rules": {
                                        "hive.shop.orders.is_test|is_test = ?": "默认需过滤测试订单"
                                    },
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    real_client_cls = httpx.Client

    def _patched_ok_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(ok_handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(
        "semantica_dataagent.query_history_miner.httpx.Client", _patched_ok_client
    )

    enriched = mine_query_history(
        records,
        tables=tables,
        min_frequency=3,
        enable_llm=True,
        override_provider=provider,
        model="gpt-mock",
    )
    assert enriched.llm_used is True
    assert enriched.llm_fallback_reason is None
    assert len(captured_prompts) == 1
    assert "secret_vip" not in captured_prompts[0]
    assert "13800138000" not in captured_prompts[0]
    assert "corp.com" not in captured_prompts[0]
    assert "2026-11-10" not in captured_prompts[0]
    assert enriched.metrics[0].name == "有效订单总成交额"
    is_test_rule = next(f for f in enriched.filter_rules if f.column == "is_test")
    assert is_test_rule.instruction == "默认需过滤测试订单"

    # 2. LLM HTTP 500 failure -> deterministic offline fallback succeeds completely
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "upstream unavailable"})

    def _patched_fail_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fail_handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(
        "semantica_dataagent.query_history_miner.httpx.Client", _patched_fail_client
    )

    fallback = mine_query_history(
        records,
        tables=tables,
        min_frequency=3,
        enable_llm=True,
        override_provider=provider,
        model="gpt-mock",
    )
    assert fallback.llm_used is False
    assert fallback.llm_fallback_reason == "llm_call_failed"
    assert len(fallback.metrics) == 1
    assert fallback.metrics[0].key == "gmv"
    assert fallback.metrics[0].expression == "SUM(amount)"
    assert len(fallback.filter_rules) >= 1


def test_mine_handles_bare_table_with_db_cte_malicious_alias_and_no_provider_fallback(
    tmp_path, monkeypatch
):
    from cryptography.fernet import Fernet
    from semantica_dataagent import knowledge
    from semantica_dataagent.cli import initialize

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()

    tables = _sample_tables()
    monkeypatch.setattr(knowledge, "describe", lambda **kwargs: tables)

    # 1-part bare table name resolved via record.database + malicious alias with secret
    records = [
        QueryHistoryRecord(
            sql=(
                f"WITH recent AS (SELECT user_id, amount, is_test FROM orders WHERE dt = '2026-11-0{i + 1}' AND is_test = 0) "
                f'SELECT SUM(amount) AS "sk-leak12345678901234" FROM recent'
            ),
            database="shop",
            caller=f"dev_{i}",
        )
        for i in range(3)
    ]
    result = mine_query_history(
        records,
        tables=None,
        project_id="default",
        min_frequency=3,
        enable_llm=True,
    )
    assert result.stats["retained_templates"] == 1
    assert "sk-leak12345678901234" not in result.templates[0].template_sql
    assert result.templates[0].tables == ["hive.shop.orders"]
    assert any(f.column == "is_test" for f in result.filter_rules)
    assert result.llm_used is False
    assert result.llm_fallback_reason == "no_provider_configured"
