"""Historical ticket content cannot reproduce an evaluation question (#350)."""


def check_issue_350_content_gate(ctx):
    created = ctx.rest.post(
        "/api/v1/projects", json={"name": f"i350 {ctx.generation}", "type": "warehouse"}
    )
    ctx.require(created.status_code == 201, created.text)
    project = created.json()["id"]
    params = {"project": project}
    question = "基准问题：昨日支付单量"
    benchmark = ctx.rest.post(
        "/api/v1/eval-questions",
        params=params,
        json={
            "id": "i350",
            "question": question,
            "caliber": "按支付时间统计",
            "reference_sql": "SELECT COUNT(*) FROM dm.dm_paid_orders",
            "dialect": "doris",
            "split": "holdout",
            "answer_confirmed": True,
        },
    )
    ctx.require(benchmark.status_code == 201, benchmark.text)
    response = ctx.rest.post(
        "/api/v1/historical-tickets",
        params=params,
        json={"title": "不同标题", "content": f"# {question}\n"},
    )
    ctx.require(response.status_code == 409, response.text)
