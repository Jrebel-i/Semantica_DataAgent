import json

import pytest

from semantica_dataagent.eval import (
    caliber_hit,
    load_questions,
    run_evaluation,
    score_question,
    table_recall,
    write_reports,
)

QUESTIONS = """\
- id: q1
  question: 最近一年有多少订单？
  expected_tables: [shop.orders]
  expected_fields: [shop.orders.id, shop.orders.dt]
  caliber: 订单数按订单编号去重，按 dt 过滤
  reference_sql: SELECT COUNT(DISTINCT id) FROM shop.orders WHERE dt >= '2025-01-01'
  notes: 出处：samples/demo/tables.json、samples/demo/fields.json
- id: q2
  question: 哪些订单发生过支付？
  expected_tables: [shop.orders, shop.payments]
  expected_fields: [shop.orders.id, shop.payments.id]
  caliber: 订单与支付流水通过业务编号关联
  reference_sql: SELECT id FROM shop.orders WHERE id IN (SELECT id FROM shop.payments)
  notes: 出处：samples/demo/tables.json
- id: q3
  question: 安卓渠道有多少访问？
  expected_tables: [shop.channels]
  expected_fields: [shop.channels.id, shop.channels.os_type]
  caliber: 安卓渠道的 os_type 为 android
  reference_sql: SELECT id FROM shop.channels WHERE os_type = 'android'
  notes: 出处：samples/demo/dimension_values.json
"""


@pytest.fixture
def questions(tmp_path):
    path = tmp_path / "questions.yaml"
    path.write_text(QUESTIONS, encoding="utf-8")
    return load_questions(path)


def table(database, name):
    return {
        "ref": f"table:{database}.{name}",
        "type": "table",
        "database": database,
        "name": name,
    }


def doc(ref):
    return {"ref": f"document:{ref}", "type": "document"}


def test_load_questions_requires_the_documented_shape(tmp_path):
    questions = load_questions(tmp_path / "unused.yaml", text=QUESTIONS)
    assert [q.id for q in questions] == ["q1", "q2", "q3"]
    assert questions[1].expected_tables == ["shop.orders", "shop.payments"]
    assert questions[0].dialect == "hive"

    broken = tmp_path / "broken.yaml"
    broken.write_text("- id: broken\n  question: 只有问题\n", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.*expected_tables"):
        load_questions(broken)


def test_table_recall_at_five_counts_tables_not_documents_or_duplicates(questions):
    hits = [
        table("shop", "orders"),
        doc("metrics/orders.md"),
        table("shop", "orders"),
        table("shop", "payments"),
        table("shop", "refunds"),
        table("shop", "orders"),
    ]
    recall, retrieved, missing = table_recall(
        ["shop.orders", "shop.payments"], hits, top_k=5
    )
    assert recall == 1.0
    assert retrieved == ["shop.orders", "shop.payments", "shop.refunds"]
    assert missing == []

    recall, _, missing = table_recall(["shop.orders", "shop.customers"], hits, 2)
    assert recall == 0.5
    assert missing == ["shop.customers"]


def test_caliber_hit_checks_tables_fields_and_key_filters(questions):
    question = questions[2]
    assert (
        caliber_hit(
            question,
            ["SELECT id FROM shop.channels WHERE os_type = 'android'"],
        )
        is True
    )
    assert caliber_hit(question, ["SELECT id FROM shop.channels"]) is False
    assert (
        caliber_hit(question, ["SELECT id FROM shop.orders WHERE os_type = 'android'"])
        is False
    )
    assert (
        caliber_hit(question, ["SELECT id FROM shop.channels WHERE os_type = 'ios'"])
        is False
    )
    assert (
        caliber_hit(
            question, ["SELECT os_name FROM shop.channels WHERE os_type = 'android'"]
        )
        is False
    )


def test_score_question_reports_every_failure_reason(questions):
    question = questions[1]
    result = score_question(
        question,
        hits=[table("shop", "orders")],
        validation={"status": "issues", "syntax": {"status": "pass"}},
        answer_sqls=["SELECT id FROM shop.orders"],
        mode="agent",
        top_k=5,
    )
    assert result["table_recall_at_5"] == 0.5
    assert result["retrieved_tables"] == ["shop.orders"]
    assert result["missing_tables"] == ["shop.payments"]
    assert result["validate_sql"]["valid"] is False
    assert result["caliber_hit"] is False
    assert result["failure_reasons"] == [
        "table recall@5 0.50 < 1.00: missing shop.payments",
        "validate_sql status=issues",
        "caliber miss: expected tables/fields/filters are not all present",
    ]
    json.dumps(result, ensure_ascii=False)


def test_retrieval_evaluation_has_overall_metrics_and_one_entry_per_question(questions):
    hits = {
        "最近一年有多少订单？": [table("shop", "orders")],
        "哪些订单发生过支付？": [table("shop", "orders")],
        "安卓渠道有多少访问？": [table("shop", "channels")],
    }
    validations = {
        "SELECT COUNT(DISTINCT id) FROM shop.orders WHERE dt >= '2025-01-01'": {
            "status": "pass"
        },
        "SELECT id FROM shop.orders WHERE id IN (SELECT id FROM shop.payments)": {
            "status": "issues"
        },
        "SELECT id FROM shop.channels WHERE os_type = 'android'": {"status": "pass"},
    }
    report = run_evaluation(
        questions,
        retrieve=lambda text: hits[text],
        validate=lambda sql, dialect: validations[sql],
        mode="retrieval",
        top_k=5,
        source="questions.yaml",
    )
    assert report["mode"] == "retrieval"
    metrics = report["metrics"]
    assert metrics["questions"] == 3
    assert metrics["table_recall_at_5"] == pytest.approx(5 / 6)
    assert metrics["table_hit_rate_at_5"] == pytest.approx(2 / 3)
    assert metrics["validate_sql_pass_rate"] == pytest.approx(2 / 3)
    assert metrics["caliber_hit_rate"] is None
    assert [row["id"] for row in report["results"]] == ["q1", "q2", "q3"]
    assert report["results"][1]["failure_reasons"] == [
        "table recall@5 0.50 < 1.00: missing shop.payments",
        "validate_sql status=issues",
    ]


def test_agent_evaluation_scores_answer_sql_caliber_and_validation(questions):
    hits = {q.question: [table("shop", "orders")] for q in questions}
    hits[questions[2].question] = [table("shop", "channels")]
    answers = {
        "q1": ["SELECT COUNT(DISTINCT id) FROM shop.orders WHERE dt >= '2025-01-01'"],
        "q2": ["SELECT id FROM shop.orders"],
        "q3": ["SELECT id FROM shop.channels WHERE os_type = 'ios'"],
    }

    def validate(sql, dialect):
        missing_payment = "shop.payments" not in sql and "COUNT" not in sql
        return {"status": "issues" if "ios" in sql or missing_payment else "pass"}

    report = run_evaluation(
        questions,
        retrieve=lambda text: hits[text],
        validate=validate,
        answers=lambda question: answers[question.id],
        mode="agent",
        top_k=5,
        source="questions.yaml",
    )
    assert report["metrics"]["caliber_hit_rate"] == pytest.approx(1 / 3)
    assert report["metrics"]["validate_sql_pass_rate"] == pytest.approx(1 / 3)
    assert report["results"][2]["caliber_hit"] is False


def test_reports_are_written_as_markdown_and_json(questions, tmp_path):
    report = run_evaluation(
        questions,
        retrieve=lambda text: [table("shop", "orders")],
        validate=lambda sql, dialect: {"status": "pass"},
        mode="retrieval",
        top_k=5,
        source="questions.yaml",
    )
    markdown, payload = write_reports(report, tmp_path / "report")
    assert markdown == tmp_path / "report.md"
    assert payload == tmp_path / "report.json"
    text = markdown.read_text(encoding="utf-8")
    assert "表召回率@5" in text
    assert "validate_sql 通过率" in text
    assert "q1" in text and "q2" in text and "q3" in text
    assert "失败原因" in text
    data = json.loads(payload.read_text(encoding="utf-8"))
    assert data["metrics"]["questions"] == 3
    assert len(data["results"]) == 3
