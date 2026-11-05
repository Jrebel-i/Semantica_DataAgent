"""Exercise business term to table recall through the public search API."""


def check_business_term_recalls_table(ctx):
    response = ctx.rest.get(
        "/api/v1/search", params={"query": "销售额", "scope": "tables", "top_k": 5}
    )
    response.raise_for_status()
    hits = response.json()
    refs = [hit["ref"] for hit in hits]
    ctx.require(
        "table:warehouse.order_metrics" in refs,
        f"业务词未在前五条召回 warehouse.order_metrics：{refs}",
    )
    return {"refs": refs}
