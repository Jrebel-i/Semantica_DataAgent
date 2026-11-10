"""Acceptance tests for the #235 evaluation tooling upgrade."""

from __future__ import annotations

import json
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import yaml

from semantica_dataagent import eval as evaluation
from semantica_dataagent.cli import _parser
from semantica_dataagent.eval_exec import run_cli

GOOD = "SELECT id FROM shop.orders WHERE dt = '2025-01-01'"
BAD = "SELECT id FROM shop.orders"
BROKEN = "SELECT broken FROM shop.orders"
TRUNCATED = "SELECT id FROM shop.orders WHERE dt = '2025-01-02'"


class FakeQuestionService:
    """Small fake ask + query service; no model or data source is involved."""

    def __init__(self):
        self.asks = []
        self.executed = []
        self.plans = {}
        self.counts = Counter()
        self.responses = {}
        self.fail_sqls = set()
        self.truncated_sqls = set()
        self.clarify_questions = set()
        self.clarify_sql = {}
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def reset(
        self,
        responses=None,
        *,
        fail_sqls=None,
        truncated_sqls=None,
        clarify=None,
    ):
        self.asks.clear()
        self.executed.clear()
        self.plans.clear()
        self.counts.clear()
        self.clarify_sql.clear()
        self.responses = dict(responses or {})
        self.fail_sqls = set(fail_sqls or ())
        self.truncated_sqls = set(truncated_sqls or ())
        self.clarify_questions = set(clarify or ())

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sse(self, events):
                body = "".join(
                    f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    for name, data in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                service.asks.append((self.path, body))
                if self.path == "/api/v1/conversations/ask":
                    question = body["question"]
                    conversation_id = body.get("conversation_id")
                    if conversation_id in service.clarify_sql:
                        sql = service.clarify_sql.pop(conversation_id)
                        self._sse(
                            [
                                (
                                    "done",
                                    {
                                        "text": "已按口径生成 SQL",
                                        "sql": [{"sql": sql}] if sql else [],
                                    },
                                ),
                            ]
                        )
                        return
                    service.counts[question] += 1
                    run_number = service.counts[question]
                    conversation_id = conversation_id or f"conv-{question}-{run_number}"
                    choices = service.responses.get(question) or [GOOD]
                    sql = choices[min(run_number - 1, len(choices) - 1)]
                    if question in service.clarify_questions:
                        service.clarify_sql[conversation_id] = sql
                        self._sse(
                            [
                                (
                                    "conversation",
                                    {"conversation_id": conversation_id},
                                ),
                                (
                                    "clarification",
                                    {
                                        "questions": [
                                            {
                                                "question": "用哪个口径？",
                                                "options": [{"label": "订单口径"}],
                                            }
                                        ]
                                    },
                                ),
                                ("done", {"text": "", "sql": []}),
                            ]
                        )
                        return
                    self._sse(
                        [
                            ("conversation", {"conversation_id": conversation_id}),
                            (
                                "tool_start",
                                {"name": "search_knowledge", "summary": "检索知识"},
                            ),
                            (
                                "tool_start",
                                {"name": "propose_query", "summary": "生成 SQL"},
                            ),
                            (
                                "done",
                                {
                                    "text": "已生成 SQL",
                                    "sql": [{"sql": sql}] if sql else [],
                                },
                            ),
                        ]
                    )
                elif self.path == "/api/v1/queries/plan":
                    if body["sql"] in service.fail_sqls:
                        self._json(422, {"detail": "参考 SQL 无法执行"})
                        return
                    query_id = f"query-{len(service.plans)}"
                    service.plans[query_id] = body["sql"]
                    self._json(200, {"id": query_id, "execution_limit": 10})
                elif self.path.startswith("/api/v1/queries/") and self.path.endswith(
                    "/execute"
                ):
                    query_id = self.path.split("/")[4]
                    sql = service.plans[query_id]
                    service.executed.append(sql)
                    rows = [[1]] if sql == GOOD else [[1], [2]]
                    row_count = (
                        len(rows) + 1 if sql in service.truncated_sqls else len(rows)
                    )
                    self._json(
                        200,
                        {
                            "columns": ["value"],
                            "rows": rows,
                            "row_count": row_count,
                        },
                    )
                else:
                    self._json(404, {"detail": "not found"})

        return Handler


def _question(
    question_id,
    question,
    *,
    category="选表",
    split="dev",
    confirmed=True,
    reference_sql=GOOD,
):
    row = {
        "id": question_id,
        "question": question,
        "expected_tables": ["shop.orders"],
        "expected_fields": ["id"],
        "caliber": "按订单口径",
        "reference_sql": reference_sql,
        "category": category,
        "split": split,
    }
    if confirmed is not None:
        row["answer_confirmed"] = confirmed
    return row


def _args(tmp_path, path, *, split="dev", repeat=1, only=None, name="report"):
    return type(
        "Args",
        (),
        {
            "questions": path,
            "report": tmp_path / name,
            "base_url": None,
            "token": "EVAL_EXEC_TOKEN",
            "only": only,
            "max_queries": 200,
            "skip_reference": False,
            "agent": "pi",
            "provider_id": "fake",
            "model": "fake",
            "project": "eval",
            "split": split,
            "repeat": repeat,
            "expected_categories": None,
        },
    )()


def _write_questions(tmp_path, rows, name="questions.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(rows, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def service():
    fake = FakeQuestionService()
    try:
        yield fake
    finally:
        fake.close()


def test_eval_exec_defaults_to_dev_and_lists_unconfirmed(
    service, tmp_path, monkeypatch
):
    service.reset(
        {
            "dev good": [GOOD],
            "dev bad": [BAD],
            "holdout": [GOOD],
            "unconfirmed": [GOOD],
        }
    )
    path = _write_questions(
        tmp_path,
        [
            _question("d1", "dev good", category="选表"),
            _question("d2", "dev bad", category="口径"),
            _question("h1", "holdout", category="选表", split="holdout"),
            _question("u1", "unconfirmed", category="口径", confirmed=False),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _parser().parse_args(
        [
            "eval-exec",
            "--questions",
            str(path),
            "--report",
            str(tmp_path / "report"),
            "--base-url",
            service.base_url,
            "--token",
            "EVAL_EXEC_TOKEN",
            "--agent",
            "pi",
            "--provider-id",
            "fake",
            "--model",
            "fake",
        ]
    )
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["split"] == "dev"
    assert report["repeat"] == 1
    assert report["excluded_unconfirmed"] == ["u1"]
    assert [entry["id"] for entry in report["questions"]] == ["d1", "d2"]
    asked = [body["question"] for path, body in service.asks if path.endswith("/ask")]
    assert asked == ["dev good", "dev bad"]

    entry = report["questions"][0]
    assert entry["category"] == "选表"
    assert entry["split"] == "dev"
    assert entry["answer_confirmed"] is True
    assert entry["reference_sql"] == GOOD
    assert entry["sql"] == GOOD
    assert entry["actual_sql"] == GOOD
    assert entry["correct"] is True
    assert entry["volatile"] is False
    assert entry["failure_category"] is None
    run = entry["runs"][0]
    assert run["sql"] == GOOD
    assert run["actual_sql"] == GOOD
    assert run["reference_sql"] == GOOD
    assert run["consistency"] == "equal"
    assert run["tool_call_sequence"] == ["search_knowledge", "propose_query"]
    assert run["conversation_id"].startswith("conv-dev good-")
    assert [item["name"] for item in run["tool_calls"]] == [
        "search_knowledge",
        "propose_query",
    ]
    assert run["comparison"] == "equal"
    assert report["questions"][1]["correct"] is False
    assert report["metrics"]["correct"] == 1
    assert report["metrics"]["wrong"] == 1
    assert report["coverage"]["选表"]["total"] == 1
    assert report["coverage"]["口径"]["total"] == 1
    assert report["coverage_gaps"] == [
        "维度值筛选",
        "时间语义",
        "口径歧义",
        "合法空结果",
        "多表 Join",
    ]
    markdown = (tmp_path / "report.md").read_text()
    assert "参考 SQL" in markdown
    assert "工具调用" in markdown
    assert "对话" in markdown
    assert "未确认答案" in markdown
    assert "u1" in markdown
    assert "secret" not in markdown

    service.reset({"holdout": [GOOD], "dev good": [GOOD], "dev bad": [GOOD]})
    args = _args(tmp_path, path, split="holdout", name="holdout-report")
    args.base_url = service.base_url
    assert run_cli(args) == 0
    held = json.loads((tmp_path / "holdout-report.json").read_text())
    assert [entry["id"] for entry in held["questions"]] == ["h1"]


def test_repeat_majority_volatile_and_tie(service, tmp_path, monkeypatch):
    service.reset(
        {
            "three": [GOOD, GOOD, GOOD],
            "two": [GOOD, GOOD, BAD],
            "one": [GOOD, BAD, BAD],
        }
    )
    path = _write_questions(
        tmp_path,
        [
            _question("q3", "three"),
            _question("q2", "two"),
            _question("q1", "one"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, repeat=3)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    by_id = {entry["id"]: entry for entry in report["questions"]}
    assert (by_id["q3"]["equal_runs"], by_id["q3"]["correct"]) == (3, True)
    assert by_id["q3"]["volatile"] is False
    assert (by_id["q2"]["equal_runs"], by_id["q2"]["correct"]) == (2, True)
    assert by_id["q2"]["volatile"] is True
    assert (by_id["q1"]["equal_runs"], by_id["q1"]["correct"]) == (1, False)
    assert by_id["q1"]["volatile"] is True
    assert by_id["q2"]["result"] == "correct"
    assert by_id["q1"]["result"] == "wrong"
    assert report["metrics"]["runs"] == 9
    assert report["metrics"]["correct"] == 2
    assert report["metrics"]["wrong"] == 1
    assert report["metrics"]["volatile"] == 2
    conversation_ids = [
        run["conversation_id"] for entry in report["questions"] for run in entry["runs"]
    ]
    assert len(conversation_ids) == len(set(conversation_ids)) == 9
    assert all(
        run["tool_calls"] for entry in report["questions"] for run in entry["runs"]
    )

    service.reset({"tie": [GOOD, BAD]})
    args = _args(tmp_path, path, repeat=2, name="tie-report")
    tie_path = _write_questions(tmp_path, [_question("tie", "tie")])
    args.questions = tie_path
    args.base_url = service.base_url
    assert run_cli(args) == 0
    tie = json.loads((tmp_path / "tie-report.json").read_text())["questions"][0]
    assert tie["result"] == "tie"
    assert tie["correct"] is False
    assert tie["volatile"] is True
    assert tie["equal_runs"] == 1


def test_split_is_reproducible_and_stratified(tmp_path):
    assign_splits = getattr(evaluation, "assign_splits", None)
    assert assign_splits is not None, "eval.assign_splits is missing"
    rows = []
    for index in range(40):
        if index < 20:
            category = "A"
        elif index < 30:
            category = "B"
        else:
            category = "C"
        rows.append(_question(f"q{index}", f"question {index}", category=category))

    first = assign_splits([dict(row) for row in rows], holdout_size=12, seed=235)
    second = assign_splits([dict(row) for row in rows], holdout_size=12, seed=235)
    other = assign_splits([dict(row) for row in rows], holdout_size=12, seed=236)
    assert first == second
    assert first != other
    holdout = [item for item in first if item["split"] == "holdout"]
    assert len(holdout) == 12
    assert Counter(item["category"] for item in holdout) == {
        "A": 6,
        "B": 3,
        "C": 3,
    }
    assert (
        sum(item["split"] == "dev" for item in first if item["category"] == "A") == 14
    )


def test_split_command_writes_reproducible_splits(tmp_path):
    try:
        args = _parser().parse_args(
            [
                "eval-split",
                "--questions",
                str(tmp_path / "questions.yaml"),
                "--holdout-size",
                "4",
                "--seed",
                "17",
            ]
        )
    except SystemExit:
        pytest.fail("eval-split command is missing")
    rows = [
        _question(f"q{index}", f"question {index}", category=index % 2 and "A" or "B")
        for index in range(10)
    ]
    path = _write_questions(tmp_path, rows)
    args.questions = path
    run_split = getattr(evaluation, "run_split", None)
    assert run_split is not None, "eval.run_split is missing"
    assert run_split(args) == 0
    payload = yaml.safe_load(path.read_text())
    first = {row["id"]: row["split"] for row in payload}
    assert Counter(first.values()) == {"dev": 6, "holdout": 4}
    assert all(row["split"] in {"dev", "holdout"} for row in payload)
    assert all(row["question"].startswith("question ") for row in payload)

    assert run_split(args) == 0
    again = yaml.safe_load(path.read_text())
    assert {row["id"]: row["split"] for row in again} == first


def test_old_question_set_keeps_working(tmp_path, service, monkeypatch):
    legacy = {
        "id": "legacy",
        "question": "legacy question",
        "expected_tables": ["shop.orders"],
        "expected_fields": ["id"],
        "caliber": "旧口径",
        "reference_sql": GOOD,
    }
    path = _write_questions(tmp_path, [legacy])
    loaded = evaluation.load_questions(path)
    assert loaded[0].split == "dev"
    assert loaded[0].answer_confirmed is True
    assert loaded[0].category == "未分类"

    service.reset({"legacy question": [GOOD]})
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path)
    args.base_url = service.base_url
    assert run_cli(args) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["questions"][0]["id"] == "legacy"
    assert report["questions"][0]["correct"] is True
    assert report["excluded_unconfirmed"] == []


def test_failure_categories_merge_back_and_recompute(tmp_path, service, monkeypatch):
    service.reset({"good": [GOOD], "bad": [BAD]})
    path = _write_questions(
        tmp_path,
        [
            _question("good", "good"),
            _question("bad", "bad", category="口径"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path)
    args.base_url = service.base_url
    assert run_cli(args) == 0
    categories = tmp_path / "categories.yaml"
    categories.write_text(
        yaml.safe_dump({"good": "选错表", "bad": "口径错"}, allow_unicode=True),
        encoding="utf-8",
    )
    try:
        merge_args = _parser().parse_args(
            [
                "eval-report",
                "--report",
                str(tmp_path / "report"),
                "--categories",
                str(categories),
            ]
        )
    except SystemExit:
        pytest.fail("eval-report command is missing")
    run_report = getattr(
        __import__("semantica_dataagent.eval_exec", fromlist=["x"]), "run_report", None
    )
    assert run_report is not None, "eval_exec.run_report is missing"
    assert run_report(merge_args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    by_id = {entry["id"]: entry for entry in report["questions"]}
    assert by_id["good"]["failure_category"] == "选错表"
    assert by_id["bad"]["failure_category"] == "口径错"
    assert report["failure_categories"] == {"选错表": 1, "口径错": 1}
    assert report["metrics"]["classified"] == 2
    assert report["metrics"]["unclassified"] == 0
    markdown = (tmp_path / "report.md").read_text()
    assert "选错表" in markdown
    assert "口径错" in markdown


def test_invalid_failure_category_is_rejected(tmp_path):
    categories = tmp_path / "categories.yaml"
    categories.write_text(yaml.safe_dump({"q1": "瞎填"}), encoding="utf-8")
    (tmp_path / "report.json").write_text(
        json.dumps({"questions": [{"id": "q1", "failure_category": None}]}),
        encoding="utf-8",
    )
    merge_args = _parser().parse_args(
        [
            "eval-report",
            "--report",
            str(tmp_path / "report"),
            "--categories",
            str(categories),
        ]
    )
    run_report = getattr(
        __import__("semantica_dataagent.eval_exec", fromlist=["x"]), "run_report", None
    )
    assert run_report is not None, "eval_exec.run_report is missing"
    with pytest.raises(SystemExit):
        run_report(merge_args)


def test_split_defaults_to_28_and_12_for_40_questions():
    assign_splits = getattr(evaluation, "assign_splits", None)
    assert assign_splits is not None, "eval.assign_splits is missing"
    rows = [
        _question(f"q{index}", f"question {index}", category="A") for index in range(40)
    ]
    result = assign_splits(rows)
    assert Counter(item["split"] for item in result) == {"dev": 28, "holdout": 12}


def test_only_smoke_still_works_inside_the_selected_split(
    service, tmp_path, monkeypatch
):
    service.reset({"dev good": [GOOD], "dev other": [GOOD], "holdout": [GOOD]})
    path = _write_questions(
        tmp_path,
        [
            _question("d1", "dev good"),
            _question("d2", "dev other"),
            _question("h1", "holdout", split="holdout"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, only="d1")
    args.base_url = service.base_url
    assert run_cli(args) == 0
    asked = [body["question"] for path, body in service.asks if path.endswith("/ask")]
    assert asked == ["dev good"]

    blocked = _args(tmp_path, path, only="h1", name="blocked")
    blocked.base_url = service.base_url
    with pytest.raises(SystemExit):
        run_cli(blocked)


def test_majority_verdict_and_displayed_sql(service, tmp_path, monkeypatch):
    service.reset(
        {
            "late": [BAD, GOOD, GOOD],
            "early": [GOOD, BAD, BAD],
        }
    )
    path = _write_questions(
        tmp_path,
        [
            _question("late", "late"),
            _question("early", "early"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, repeat=3)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    by_id = {entry["id"]: entry for entry in report["questions"]}
    late = by_id["late"]
    assert late["equal_runs"] == 2
    assert late["result"] == "correct"
    assert late["comparison"] == "equal"
    assert late["consistency"] == "equal"
    assert late["summary"] == "行数、列数及结果一致"
    assert late["sql"] == GOOD
    assert late["actual_sql"] == GOOD
    assert late["conversation_id"] == "conv-late-2"
    assert late["runs"][0]["sql"] == BAD

    early = by_id["early"]
    assert early["equal_runs"] == 1
    assert early["result"] == "wrong"
    assert early["comparison"] == "different"
    assert early["consistency"] == "different"
    assert early["summary"] == "行数、列数或结果不同"
    assert early["sql"] == BAD
    assert early["actual_sql"] == BAD
    assert early["conversation_id"] == "conv-early-2"
    assert early["runs"][0]["sql"] == GOOD

    metrics = report["metrics"]
    assert metrics["equal"] == 1
    assert metrics["comparable"] == 2
    assert metrics["consistency_rate"] == 0.5
    assert metrics["correct"] == 1
    assert metrics["wrong"] == 1
    assert "majority_consistency_rate" not in metrics


def test_reference_failure_and_majority_uncomparable_are_excluded(
    service, tmp_path, monkeypatch
):
    service.reset(
        {
            "good": [GOOD, GOOD, GOOD],
            "reference broken": [GOOD, GOOD, GOOD],
            "mostly truncated": [GOOD, TRUNCATED, TRUNCATED],
        },
        fail_sqls={BROKEN},
        truncated_sqls={TRUNCATED},
    )
    path = _write_questions(
        tmp_path,
        [
            _question("good", "good"),
            _question("qref", "reference broken", reference_sql=BROKEN),
            _question("qtrunc", "mostly truncated"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, repeat=3)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    by_id = {entry["id"]: entry for entry in report["questions"]}
    assert by_id["good"]["result"] == "correct"
    for question_id in ("qref", "qtrunc"):
        entry = by_id[question_id]
        assert entry["result"] == "uncomparable"
        assert entry["comparison"] == "uncomparable"
        assert entry["correct"] is False
        assert entry["summary"] is None
    assert by_id["qref"]["reference"]["status"] == "failed"

    metrics = report["metrics"]
    assert metrics["equal"] == 1
    assert metrics["comparable"] == 1
    assert metrics["consistency_rate"] == 1.0
    assert metrics["correct"] == 1
    assert metrics["wrong"] == 0
    assert metrics["tie"] == 0
    assert metrics["uncomparable"] == 2
    assert metrics["reference_failures"] == 1
    reasons = {item["id"]: item["reason"] for item in report["excluded_uncomparable"]}
    assert reasons == {
        "qref": "reference_failed",
        "qtrunc": "majority_uncomparable",
    }
    markdown = (tmp_path / "report.md").read_text()
    assert "不可比题目" in markdown
    assert "qref" in markdown and "qtrunc" in markdown


def test_repeat_one_metrics_keep_previous_numbers(service, tmp_path, monkeypatch):
    service.reset({"good": [GOOD], "bad": [BAD]})
    path = _write_questions(
        tmp_path,
        [_question("good", "good"), _question("bad", "bad")],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    metrics = json.loads((tmp_path / "report.json").read_text())["metrics"]
    assert metrics["questions"] == 2
    assert metrics["runs"] == 2
    assert metrics["equal"] == 1
    assert metrics["comparable"] == 2
    assert metrics["consistency_rate"] == 0.5
    assert metrics["correct"] == 1
    assert metrics["wrong"] == 1
    assert metrics["tie"] == 0
    assert metrics["uncomparable"] == 0
    assert "majority_consistency_rate" not in metrics


def test_coverage_gaps_use_expected_categories(service, tmp_path, monkeypatch):
    service.reset({"wrong": [BAD], "good": [GOOD]})
    path = _write_questions(
        tmp_path,
        [
            _question("wrong", "wrong", category="多表 Join"),
            _question("good", "good", category="选表"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["coverage"]["多表 Join"]["total"] == 1
    assert report["coverage"]["多表 Join"]["correct"] == 0
    assert report["coverage_gaps"] == [
        "维度值筛选",
        "时间语义",
        "口径歧义",
        "合法空结果",
    ]
    assert "多表 Join" not in report["coverage_gaps"]

    args = _args(tmp_path, path, name="override")
    args.base_url = service.base_url
    args.expected_categories = "多表 Join,选表,时间语义"
    assert run_cli(args) == 0
    override = json.loads((tmp_path / "override.json").read_text())
    assert override["expected_categories"] == ["多表 Join", "选表", "时间语义"]
    assert override["coverage_gaps"] == ["时间语义"]

    parsed = _parser().parse_args(
        [
            "eval-exec",
            "--questions",
            str(path),
            "--report",
            str(tmp_path / "parsed"),
            "--base-url",
            service.base_url,
            "--token",
            "EVAL_EXEC_TOKEN",
            "--agent",
            "pi",
            "--provider-id",
            "fake",
            "--model",
            "fake",
            "--expected-categories",
            "A, B ,A",
        ]
    )
    assert parsed.expected_categories == "A, B ,A"


def test_run_level_metrics_and_question_flags(service, tmp_path, monkeypatch):
    service.reset(
        {
            "mixed": [None, GOOD, BAD],
            "clarify": [GOOD, GOOD, GOOD],
        },
        clarify={"clarify"},
    )
    path = _write_questions(
        tmp_path,
        [_question("mixed", "mixed"), _question("clarify", "clarify")],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, repeat=3)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    by_id = {entry["id"]: entry for entry in report["questions"]}
    assert by_id["mixed"]["result"] == "tie"
    assert by_id["mixed"]["clarified"] is False
    assert by_id["mixed"]["clarified_runs"] == 0
    assert by_id["clarify"]["result"] == "correct"
    assert by_id["clarify"]["clarified"] is True
    assert by_id["clarify"]["clarified_runs"] == 3

    metrics = report["metrics"]
    assert metrics["execution_total"] == 5
    assert metrics["execution_success"] == 5
    assert metrics["clarified"] == 1
    assert metrics["clarified_runs"] == 3
    assert metrics["no_sql"] == 1
    assert metrics["no_sql_runs"] == 1
    assert metrics["via_plan"] == 0
    assert metrics["via_plan_runs"] == 0
    assert metrics["condition_incomplete"] == 1
    assert metrics["condition_incomplete_runs"] == 1
