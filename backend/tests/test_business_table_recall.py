"""Business terms in documents can retrieve their associated warehouse tables."""

from semantica_dataagent import knowledge


def test_document_business_term_recalls_english_table(monkeypatch):
    document = {
        "ref": "document:metrics/sales.md#chunk-1",
        "doc_id": "document:metrics/sales.md",
        "content": "销售额：SUM(amount)，来源表 warehouse.order_metrics。",
        "scope": "docs",
    }
    table = {
        "ref": "table:warehouse.order_metrics",
        "database": "warehouse",
        "name": "order_metrics",
        "comment": "order metrics",
    }

    class Vectors:
        def search(self, **kwargs):
            return (
                [[{"entity": {"metadata": document}}]]
                if '"docs"' in kwargs["filter"]
                else [[]]
            )

    def execute(_graph, cypher, params):
        if "db.index.fulltext.queryNodes" in cypher:
            return []
        if "seeds" in params and document["ref"] in params["seeds"]:
            return [{"item": table}]
        return []

    monkeypatch.setattr(knowledge, "settings", lambda: {"knowledge_generation": "test"})
    monkeypatch.setattr(knowledge, "embed", lambda *_: [[1.0, 0.0]])
    monkeypatch.setattr(knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(knowledge, "read_vectors", lambda _: Vectors())
    monkeypatch.setattr(knowledge, "execute", execute)

    hits = knowledge.search("销售额", "tables", 5)
    assert table["ref"] in [hit["ref"] for hit in hits], "业务词未召回文档关联的数仓表"


def test_document_relations_include_quoted_sql_table_names():
    from semantica_dataagent.canonical import Record
    from semantica_dataagent.relationships import reconcile

    class Storage:
        build_id = 1

        def __init__(self):
            self.links = []

        def execute(self, query, params):
            if "UNWIND $rows AS row MATCH (d:SDADocument" in query:
                self.links.extend(params["rows"])

    storage = Storage()
    records = [
        Record("table", {"database": "warehouse", "name": "order_metrics"}),
        Record(
            "document",
            {
                "category": "metrics",
                "path": "sales.md",
                "content": '销售额来自 FROM `warehouse`.`order_metrics` JOIN "warehouse"."unknown"',
            },
        ),
    ]
    reconcile(storage, records, "test")
    assert [(row["database"], row["name"]) for row in storage.links] == [
        ("warehouse", "order_metrics")
    ]


def test_direct_table_matches_outrank_frequently_mentioned_table(monkeypatch):
    inventory = {"ref": "table:shop.inventory", "name": "inventory", "comment": "库存"}
    hub = {"ref": "table:shop.orders", "name": "orders", "comment": "订单"}
    documents = [
        {"ref": f"document:metrics/{i}#chunk-1", "doc_id": f"document:metrics/{i}"}
        for i in range(10)
    ]

    class Vectors:
        def search(self, **kwargs):
            return [
                [
                    {"entity": {"metadata": item}}
                    for item in [*documents, hub, inventory]
                ]
            ]

    def execute(_graph, cypher, params):
        if "db.index.fulltext.queryNodes" in cypher:
            return [{"source": "fulltext", "item": inventory}]
        if "seeds" in params:
            return [
                {"seed_ref": document["ref"], "item": hub} for document in documents
            ]
        return []

    monkeypatch.setattr(knowledge, "settings", lambda: {"knowledge_generation": "test"})
    monkeypatch.setattr(knowledge, "embed", lambda *_: [[1.0, 0.0]])
    monkeypatch.setattr(knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(knowledge, "read_vectors", lambda _: Vectors())
    monkeypatch.setattr(knowledge, "execute", execute)

    hits = knowledge.search("库存周转", "tables", 5)
    assert hits[0]["ref"] == inventory["ref"], (
        f"直接命中的库存表被文档提及的表压过：{hits}"
    )
