"""B5 regression tests: override dedup must honour catalog and relationship shape."""

from __future__ import annotations

from typing import Any, Self

import pytest

from semantica_dataagent import knowledge_synthesizer, semantic_overrides


def _index(
    catalogs: tuple[str, ...],
    *,
    tables: tuple[tuple[str, str], ...] = (
        ("shop", "orders"),
        ("shop", "customers"),
    ),
) -> semantic_overrides._SchemaIndex:
    return semantic_overrides._SchemaIndex.from_described(
        [
            {
                "catalog": catalog,
                "database": database,
                "name": name,
                "fields": [
                    {"name": "id"},
                    {"name": "customer_id"},
                    {"name": "region"},
                    {"name": "region_id"},
                ],
            }
            for catalog in catalogs
            for database, name in tables
        ]
    )


def _rel(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": "orders_customers",
        "from_database": "shop",
        "from_table": "orders",
        "from_columns": ["customer_id"],
        "to_database": "shop",
        "to_table": "customers",
        "to_columns": ["id"],
    }
    entry.update(overrides)
    return entry


def test_issue_345_table_and_field_keys_resolve_to_same_physical_table():
    index = _index(("hive",))
    existing = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "fields": {
            "hive.shop.orders.amount": {
                "catalog": "hive",
                "database": "shop",
                "table": "orders",
                "name": "amount",
                "comment": "订单金额",
            }
        },
    }
    candidate = {
        "version": 1,
        "tables": {
            "shop.orders": {
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "fields": {
            "shop.orders.amount": {
                "database": "shop",
                "table": "orders",
                "name": "amount",
                "comment": "订单金额",
            }
        },
    }

    assert (
        knowledge_synthesizer._override_matches_existing(existing, candidate, index)
        is True
    )


def test_issue_345_relationship_column_order_is_normalized():
    index = _index(("hive",))
    left = _rel(
        from_table="customers",
        from_columns=["id", "region"],
        to_table="orders",
        to_columns=["customer_id", "region_id"],
    )
    right = _rel(
        from_table="customers",
        from_columns=["region", "id"],
        to_table="orders",
        to_columns=["region_id", "customer_id"],
    )

    assert knowledge_synthesizer._norm_rel_pair(left, index) == (
        knowledge_synthesizer._norm_rel_pair(right, index)
    )
    assert (
        knowledge_synthesizer._overrides_overlap(
            {"relationships": {"a": left}},
            {"relationships": {"b": right}},
            index,
        )
        is True
    )


def test_issue_345_crossed_column_pairs_are_not_duplicates():
    index = _index(("hive",))
    left = _rel(
        from_columns=["customer_id", "region_id"],
        to_columns=["id", "region"],
    )
    crossed = _rel(
        from_columns=["customer_id", "region_id"],
        to_columns=["region", "id"],
    )
    left_doc = {"version": 1, "relationships": {"orders_customers": left}}
    crossed_doc = {"version": 1, "relationships": {"orders_customers": crossed}}

    assert knowledge_synthesizer._norm_rel_pair(
        left, index
    ) != knowledge_synthesizer._norm_rel_pair(crossed, index)
    assert (
        knowledge_synthesizer._overrides_overlap(left_doc, crossed_doc, index) is False
    )
    assert (
        knowledge_synthesizer._override_matches_existing(left_doc, crossed_doc, index)
        is False
    )


def test_issue_345_paired_column_reorder_is_duplicate():
    index = _index(("hive",))
    left = _rel(
        from_columns=["customer_id", "region_id"],
        to_columns=["id", "region"],
    )
    reordered = _rel(
        from_columns=["region_id", "customer_id"],
        to_columns=["region", "id"],
    )
    left_doc = {"version": 1, "relationships": {"orders_customers": left}}
    reordered_doc = {"version": 1, "relationships": {"orders_customers": reordered}}

    assert knowledge_synthesizer._norm_rel_pair(
        left, index
    ) == knowledge_synthesizer._norm_rel_pair(reordered, index)
    assert (
        knowledge_synthesizer._overrides_overlap(left_doc, reordered_doc, index) is True
    )
    assert (
        knowledge_synthesizer._override_matches_existing(left_doc, reordered_doc, index)
        is True
    )


def test_issue_345_relationship_direction_is_normalized():
    index = _index(("hive",))
    forward = _rel(from_catalog="hive", to_catalog="hive")
    backward = {
        "name": "orders_customers",
        "from_catalog": "hive",
        "from_database": "shop",
        "from_table": "customers",
        "from_columns": ["id"],
        "to_catalog": "hive",
        "to_database": "shop",
        "to_table": "orders",
        "to_columns": ["customer_id"],
    }

    assert knowledge_synthesizer._norm_rel_pair(
        forward, index
    ) == knowledge_synthesizer._norm_rel_pair(backward, index)


def test_issue_345_different_catalogs_are_not_duplicates():
    index = _index(("hive", "internal"))
    hive = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
    }
    internal = {
        "version": 1,
        "tables": {
            "internal.shop.orders": {
                "catalog": "internal",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
    }
    assert (
        knowledge_synthesizer._override_matches_existing(hive, internal, index) is False
    )

    hive_rel = _rel(from_catalog="hive", to_catalog="hive")
    internal_rel = _rel(from_catalog="internal", to_catalog="internal")
    assert knowledge_synthesizer._norm_rel_pair(
        hive_rel, index
    ) != knowledge_synthesizer._norm_rel_pair(internal_rel, index)
    assert (
        knowledge_synthesizer._overrides_overlap(
            {"relationships": {"a": hive_rel}},
            {"relationships": {"b": internal_rel}},
            index,
        )
        is False
    )


def test_issue_345_same_key_with_different_catalog_is_not_duplicate():
    index = _index(("hive", "internal"))
    existing = {
        "version": 1,
        "tables": {
            "shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "relationships": {
            "orders_customers": _rel(from_catalog="hive", to_catalog="hive")
        },
    }
    candidate = {
        "version": 1,
        "tables": {
            "shop.orders": {
                "catalog": "internal",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "relationships": {
            "orders_customers": _rel(from_catalog="internal", to_catalog="internal")
        },
    }

    assert (
        knowledge_synthesizer._override_matches_existing(existing, candidate, index)
        is False
    )


def test_issue_345_ambiguous_catalogless_ref_is_not_merged():
    index = _index(("hive", "internal"))
    existing = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "relationships": {"rel_a": _rel()},
    }
    candidate = {
        "version": 1,
        "tables": {
            "shop.orders": {
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "relationships": {"rel_b": _rel()},
    }

    assert (
        knowledge_synthesizer._override_matches_existing(existing, candidate, index)
        is False
    )
    assert (
        knowledge_synthesizer._overrides_overlap(
            {"relationships": {"a": _rel()}},
            {"relationships": {"b": _rel()}},
            index,
        )
        is False
    )


def test_issue_345_unavailable_index_keeps_literal_duplicates_only():
    existing = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "relationships": {"rel_a": _rel(from_catalog="hive", to_catalog="hive")},
    }
    candidate = {
        "version": 1,
        "tables": {
            "shop.orders": {
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
        "relationships": {"rel_b": _rel()},
    }
    assert (
        knowledge_synthesizer._override_matches_existing(existing, candidate, None)
        is False
    )

    same = _rel()
    assert (
        knowledge_synthesizer._overrides_overlap(
            {"relationships": {"a": same}},
            {"relationships": {"b": dict(same)}},
            None,
        )
        is True
    )


def test_issue_345_distinct_entries_remain_distinct():
    index = _index(("hive",))
    left = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
    }
    right = {
        "version": 1,
        "tables": {
            "hive.shop.customers": {
                "catalog": "hive",
                "database": "shop",
                "name": "customers",
                "comment": "客户主表",
            }
        },
    }
    assert knowledge_synthesizer._override_matches_existing(left, right, index) is False
    assert knowledge_synthesizer._norm_rel_pair(
        _rel(), index
    ) != knowledge_synthesizer._norm_rel_pair(
        _rel(to_table="missing", to_columns=["unknown"]), index
    )


class _EmptyQuery:
    def filter(self, *args: Any, **kwargs: Any) -> _EmptyQuery:
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> _EmptyQuery:
        return self

    def all(self) -> list[Any]:
        return []


class _EmptySession:
    def query(self, *args: Any, **kwargs: Any) -> _EmptyQuery:
        return _EmptyQuery()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _empty_sessions() -> _EmptySession:
    return _EmptySession()


def _proposal(
    structured_patch: dict[str, Any], *, target: str = "table:shop.orders"
) -> Any:
    return knowledge_synthesizer.SynthesizedProposal(
        target=target,
        target_kind="override",
        resolved_path="overrides/semantic_overrides.yaml",
        is_new_file=False,
        patch="",
        structured_patch=structured_patch,
        diff="",
        reason="test",
        gate_report={},
        used_llm=False,
    )


def test_issue_345_intra_batch_uses_schema_candidates():
    index = _index(("hive",))
    previous = _proposal(
        {
            "version": 1,
            "tables": {
                "hive.shop.orders": {
                    "catalog": "hive",
                    "database": "shop",
                    "name": "orders",
                    "comment": "订单主表",
                }
            },
        }
    )
    candidate = _proposal(
        {
            "version": 1,
            "tables": {
                "shop.orders": {
                    "database": "shop",
                    "name": "orders",
                    "comment": "订单主表",
                }
            },
        }
    )

    result = knowledge_synthesizer._check_intra_batch_duplicate(
        candidate, [previous], index
    )
    assert result.is_duplicate is True


def test_issue_345_synthesize_proposals_batch_loads_schema_once(
    monkeypatch: pytest.MonkeyPatch,
):
    index = _index(("hive",))
    load_count = 0

    def fake_current_schema_index(_project_id: str) -> tuple[Any, None]:
        nonlocal load_count
        load_count += 1
        return index, None

    proposals = [
        _proposal(
            {
                "version": 1,
                "tables": {
                    "hive.shop.orders": {
                        "catalog": "hive",
                        "database": "shop",
                        "name": "orders",
                        "comment": "订单主表",
                    }
                },
            },
            target="table:hive.shop.orders",
        ),
        _proposal(
            {
                "version": 1,
                "tables": {
                    "shop.orders": {
                        "database": "shop",
                        "name": "orders",
                        "comment": "订单主表",
                    }
                },
            },
            target="table:shop.orders",
        ),
    ]
    built = iter(proposals)
    context = {
        "values": {},
        "generation": 1,
        "all_documents": [],
        "source_dir": None,
        "project_type": "semantic",
        "markdown_root": "/tmp",
        "categories": [],
        "tables": [],
        "documents": [],
        "existing_overrides": {
            "version": 1,
            "tables": {},
            "fields": {},
            "relationships": {},
            "metrics": {},
        },
    }
    monkeypatch.setattr(
        knowledge_synthesizer, "resolve_llm_provider", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        knowledge_synthesizer,
        "collect_project_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        knowledge_synthesizer,
        "_build_single_candidate",
        lambda *args, **kwargs: next(built),
    )
    monkeypatch.setattr(
        knowledge_synthesizer, "_current_schema_index", fake_current_schema_index
    )

    results = knowledge_synthesizer.synthesize_proposals(
        project_id="proj345",
        source_type="discovery",
        description="批内同表两种 key",
        llm_fn=lambda _ctx: {"proposals": [{"a": 1}, {"a": 2}]},
        allow_multi=True,
    )

    assert len(results) == 2
    assert results[0].is_duplicate is False
    assert results[1].is_duplicate is True
    assert results[1].duplicate_of == "batch_proposal:table:hive.shop.orders"
    assert load_count == 1


def test_issue_345_duplicate_check_uses_schema_candidates(
    monkeypatch: pytest.MonkeyPatch,
):
    index = _index(("hive",))
    monkeypatch.setattr(
        semantic_overrides, "_current_schema_index", lambda _project_id: (index, None)
    )
    monkeypatch.setattr(knowledge_synthesizer, "sessions", lambda: _empty_sessions)
    existing = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
    }
    candidate = {
        "version": 1,
        "tables": {
            "shop.orders": {
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
    }

    result = knowledge_synthesizer.check_duplicate_candidate(
        "proj345",
        target="table:shop.orders",
        target_kind="override",
        resolved_path="overrides/semantic_overrides.yaml",
        patch="",
        structured_patch=candidate,
        reason="重复表注释",
        existing_overrides=existing,
    )
    assert result.is_duplicate is True
    assert result.duplicate_of == "existing_knowledge"


def test_issue_345_duplicate_check_is_conservative_when_index_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        semantic_overrides,
        "_current_schema_index",
        lambda _project_id: (None, "storage unavailable"),
    )
    monkeypatch.setattr(knowledge_synthesizer, "sessions", lambda: _empty_sessions)
    existing = {
        "version": 1,
        "tables": {
            "hive.shop.orders": {
                "catalog": "hive",
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
    }
    candidate = {
        "version": 1,
        "tables": {
            "shop.orders": {
                "database": "shop",
                "name": "orders",
                "comment": "订单主表",
            }
        },
    }

    result = knowledge_synthesizer.check_duplicate_candidate(
        "proj345",
        target="table:shop.orders",
        target_kind="override",
        resolved_path="overrides/semantic_overrides.yaml",
        patch="",
        structured_patch=candidate,
        reason="无法解析 catalog",
        existing_overrides=existing,
    )
    assert result.is_duplicate is False
