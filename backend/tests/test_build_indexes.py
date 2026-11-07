"""Builds prepare graph indexes before writing either generation type."""

import pytest


@pytest.mark.parametrize("incremental", [False, True])
def test_build_prepares_indexes_before_graph_work(monkeypatch, incremental):
    from semantica_dataagent import knowledge

    calls = []

    class Graph:
        def execute_query(self, query, parameters):
            calls.append(query)
            if "RETURN count(*) AS deleted" in query:
                return {"success": True, "records": [{"deleted": 0}]}
            return {"success": True, "records": []}

        def close(self):
            pass

    class Vectors:
        client = type("Client", (), {"disconnect": lambda self: None})()
        collection = type("Collection", (), {"load": lambda self: None})()

        def create_collection(self, *args, **kwargs):
            pass

        def get_collection(self, *args):
            pass

    monkeypatch.setattr(knowledge, "graph", lambda _: Graph())
    monkeypatch.setattr(knowledge, "milvus", lambda _: Vectors())
    monkeypatch.setattr(knowledge, "embed", lambda *args: [[0.0]])
    monkeypatch.setattr(knowledge.utility, "has_collection", lambda _: False)
    values = {"knowledge_generation": "test"}
    manager = knowledge.incremental_storage if incremental else knowledge.build_storage
    with manager(values):
        pass
    indexes = [i for i, query in enumerate(calls) if query.startswith("CREATE INDEX")]
    assert indexes, "构建开始前没有创建范围索引"
    await_at = next(i for i, query in enumerate(calls) if "db.awaitIndexes" in query)
    assert max(indexes) < await_at
    assert not any("MATCH " in query for query in calls[:await_at])
