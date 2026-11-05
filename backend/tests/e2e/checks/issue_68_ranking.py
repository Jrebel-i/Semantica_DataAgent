import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "demo_check", Path(__file__).with_name("issue_25.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def check_demo_recall_and_ranking(ctx):
    for term, expected in [
        ("库存周转", "table:shop.inventory"),
        ("客户分群", "table:shop.customer_segments"),
        ("退款原因", "table:shop.refunds"),
    ]:
        response = ctx.rest.get(
            "/api/v1/search", params={"query": term, "scope": "tables", "top_k": 5}
        )
        response.raise_for_status()
        refs = [x["ref"] for x in response.json()]
        ctx.logger.line(f"{term}: {refs}")
        ctx.require(
            refs and refs[0] == expected, f"{term} 目标表 {expected} 不是第一名: {refs}"
        )
    result = module.check_retrieval_eval_on_demo(ctx)
    ctx.require(
        result["table_recall_at_5"] >= 0.98,
        f"合成题集召回率不足: {result['table_recall_at_5']}",
    )
    return result
