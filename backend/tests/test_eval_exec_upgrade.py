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
from semantica_dataagent.eval_exec import run_cli, run_report

GOOD = "SELECT id FROM shop.orders WHERE dt = '2025-01-01'"
BAD = "SELECT id FROM shop.orders"
BROKEN = "SELECT broken FROM shop.orders"
TRUNCATED = "SELECT id FROM shop.orders WHERE dt = '2025-01-02'"
EXPLORATORY = "SELECT exploratory FROM shop.orders WHERE dt = '2025-01-01'"
FINAL = "SELECT final FROM shop.orders WHERE dt = '2025-01-01'"


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
        self.clarify_exploratory = {}
        self.stream_error_questions = set()
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
        clarify_exploratory=None,
        stream_errors=None,
    ):
        self.asks.clear()
        self.executed.clear()
        self.plans.clear()
        self.counts.clear()
        self.clarify_sql.clear()
        self.clarify_exploratory = dict(clarify_exploratory or {})
        self.stream_error_questions = set(stream_errors or ())
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
                        exploratory = service.clarify_exploratory.get(question)
                        self._sse(
                            [
                                (
                                    "conversation",
                                    {"conversation_id": conversation_id},
                                ),
                                (
                                    "tool_start",
                                    {
                                        "name": "search_knowledge",
                                        "summary": "检索知识",
                                    },
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
                                (
                                    "done",
                                    {
                                        "text": "",
                                        "sql": [{"sql": exploratory}]
                                        if exploratory
                                        else [],
                                    },
                                ),
                            ]
                        )
                        return
                    events = [
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
                    if question in service.stream_error_questions:
                        events.append(("error", {"message": "fake stream error"}))
                    self._sse(events)
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
    caliber="按订单口径",
):
    row = {
        "id": question_id,
        "question": question,
        "expected_tables": ["shop.orders"],
        "expected_fields": ["id"],
        "caliber": caliber,
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
            "web_url": None,
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


@pytest.mark.parametrize(
    ("web_url", "normalized"),
    [
        ("https://analytics.example.test", "https://analytics.example.test"),
        ("https://analytics.example.test/", "https://analytics.example.test"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
    ],
)
def test_conversation_links_use_the_configured_external_web_url(
    service, tmp_path, monkeypatch, web_url, normalized
):
    service.reset({"linked": [GOOD]})
    path = _write_questions(tmp_path, [_question("linked", "linked")])
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _parser().parse_args(
        [
            "eval-exec",
            "--questions",
            str(path),
            "--report",
            str(tmp_path / "linked-report"),
            "--base-url",
            service.base_url,
            "--web-url",
            web_url,
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

    report = json.loads((tmp_path / "linked-report.json").read_text())
    expected = f"{normalized}/chat/conv-linked-1"
    assert report["web_url"] == normalized
    entry = report["questions"][0]
    assert entry["conversation_url"] == expected
    assert entry["runs"][0]["conversation_url"] == expected
    markdown = (tmp_path / "linked-report.md").read_text()
    assert expected in markdown
    assert "打开对话" in markdown
    assert service.base_url not in markdown


@pytest.mark.parametrize(
    "web_url",
    [
        "https://analytics.example.test/workbench",
        "https://analytics.example.test/workbench/",
        "https://analytics.example.test/a/b",
        "https://analytics.example.test///",
    ],
)
def test_web_url_rejects_a_path_prefix_before_running(
    service, tmp_path, monkeypatch, web_url
):
    service.reset({"linked": [GOOD]})
    path = _write_questions(tmp_path, [_question("linked", "linked")])
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _parser().parse_args(
        [
            "eval-exec",
            "--questions",
            str(path),
            "--report",
            str(tmp_path / "prefix-report"),
            "--base-url",
            service.base_url,
            "--web-url",
            web_url,
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

    with pytest.raises(SystemExit) as raised:
        run_cli(args)

    message = str(raised.value)
    assert "--web-url" in message
    assert "路径" in message
    assert "根" in message
    assert not (tmp_path / "prefix-report.json").exists()
    assert service.asks == []


def test_conversation_links_do_not_guess_an_internal_base_url(
    service, tmp_path, monkeypatch
):
    service.reset({"unlinked": [GOOD]})
    path = _write_questions(tmp_path, [_question("unlinked", "unlinked")])
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, name="unlinked-report")
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "unlinked-report.json").read_text())
    entry = report["questions"][0]
    assert report["web_url"] is None
    assert entry["conversation_url"] is None
    assert entry["runs"][0]["conversation_url"] is None
    markdown = (tmp_path / "unlinked-report.md").read_text()
    assert service.base_url not in markdown
    assert "未配置外部" in markdown


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
    assert by_id["q3"]["volatility"] == "stable"
    assert by_id["q3"]["volatility_reason"] == "comparable_runs_agree"
    assert (by_id["q2"]["equal_runs"], by_id["q2"]["correct"]) == (2, True)
    assert by_id["q2"]["volatile"] is True
    assert by_id["q2"]["volatility"] == "volatile"
    assert by_id["q2"]["volatility_reason"] == "comparable_runs_disagree"
    assert (by_id["q1"]["equal_runs"], by_id["q1"]["correct"]) == (1, False)
    assert by_id["q1"]["volatile"] is True
    assert by_id["q2"]["result"] == "correct"
    assert by_id["q1"]["result"] == "wrong"
    assert report["metrics"]["runs"] == 9
    assert report["metrics"]["correct"] == 2
    assert report["metrics"]["wrong"] == 1
    assert report["metrics"]["volatile"] == 2
    assert report["metrics"]["volatility_unknown"] == 0
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


def test_coverage_separates_the_whole_question_set_from_the_current_scope(
    service, tmp_path, monkeypatch
):
    holdout_sql = "SELECT holdout_answer FROM shop.hidden"
    service.reset({"dev only": [GOOD]})
    path = _write_questions(
        tmp_path,
        [
            _question("r01", "dev only", category="选表"),
            _question(
                "h1",
                "holdout time",
                category="时间语义",
                split="holdout",
                reference_sql=holdout_sql,
            ),
            _question(
                "h2",
                "holdout join",
                category="多表 Join",
                split="holdout",
                reference_sql=holdout_sql,
            ),
            _question(
                "u1",
                "unconfirmed dimension",
                category="维度值筛选",
                confirmed=False,
            ),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, only="r01")
    args.base_url = service.base_url
    args.expected_categories = "选表,时间语义,维度值筛选,多表 Join,口径歧义,合法空结果"
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["coverage_gaps"] == ["口径歧义", "合法空结果"]
    assert report["confirmed_coverage_gaps"] == [
        "维度值筛选",
        "口径歧义",
        "合法空结果",
    ]
    assert report["scope_coverage_gaps"] == [
        "时间语义",
        "维度值筛选",
        "多表 Join",
        "口径歧义",
        "合法空结果",
    ]
    assert report["unconfirmed_only_categories"] == ["维度值筛选"]
    assert set(report["coverage"]) == {"选表"}
    question_set = report["question_set_coverage"]
    assert question_set["includes_unconfirmed"] is True
    assert (
        question_set["total"],
        question_set["confirmed"],
        question_set["unconfirmed"],
    ) == (
        4,
        3,
        1,
    )
    assert question_set["splits"] == {"dev": 2, "holdout": 2}
    assert question_set["categories"]["维度值筛选"] == {
        "total": 1,
        "confirmed": 0,
        "unconfirmed": 1,
        "dev": 1,
        "holdout": 0,
    }
    assert question_set["categories"]["时间语义"]["holdout"] == 1
    serialized = (tmp_path / "report.json").read_text()
    markdown = (tmp_path / "report.md").read_text()
    assert holdout_sql not in serialized
    assert holdout_sql not in markdown
    assert "全部题集" in markdown
    assert "本次评测范围" in markdown
    assert "未确认" in markdown


def test_eval_report_falls_back_when_a_legacy_report_has_no_question_set(
    service, tmp_path, monkeypatch
):
    service.reset({"good": [GOOD], "bad": [BAD]})
    path = _write_questions(
        tmp_path,
        [
            _question("good", "good", category="选表"),
            _question("bad", "bad", category="口径"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    report.pop("question_set")
    report["questions"][0]["conversation_url"] = "https://old.example.test/chat/old"
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    categories = tmp_path / "categories.yaml"
    categories.write_text(yaml.safe_dump({}), encoding="utf-8")
    merge_args = _parser().parse_args(
        [
            "eval-report",
            "--report",
            str(tmp_path / "report"),
            "--categories",
            str(categories),
        ]
    )
    from semantica_dataagent.eval_exec import run_report

    assert run_report(merge_args) == 0
    updated = json.loads((tmp_path / "report.json").read_text())
    assert updated["question_set_coverage"]["source"] == "report_questions"
    assert updated["question_set_coverage"]["includes_unconfirmed"] is False
    assert (
        updated["questions"][0]["conversation_url"]
        == "https://old.example.test/chat/old"
    )
    assert updated["coverage_gaps"] == [
        "维度值筛选",
        "时间语义",
        "口径歧义",
        "合法空结果",
        "多表 Join",
    ]
    markdown = (tmp_path / "report.md").read_text()
    assert "是否计入未确认题：否" in markdown
    assert "旧报告：仅已评测题目" in markdown


def test_eval_report_rejects_a_legacy_web_url_with_a_path_prefix(
    service, tmp_path, monkeypatch
):
    service.reset({"good": [GOOD]})
    path = _write_questions(tmp_path, [_question("good", "good")])
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    report["web_url"] = "https://analytics.example.test/workbench"
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    categories = tmp_path / "categories.yaml"
    categories.write_text(yaml.safe_dump({}), encoding="utf-8")

    from semantica_dataagent.eval_exec import run_report

    merge_args = _parser().parse_args(
        [
            "eval-report",
            "--report",
            str(tmp_path / "report"),
            "--categories",
            str(categories),
        ]
    )
    with pytest.raises(SystemExit) as raised:
        run_report(merge_args)
    message = str(raised.value)
    assert "web_url" in message
    assert "路径" in message
    assert "根" in message

    rejected = _parser().parse_args(
        [
            "eval-report",
            "--report",
            str(tmp_path / "report"),
            "--categories",
            str(categories),
            "--web-url",
            "https://analytics.example.test/workbench",
        ]
    )
    with pytest.raises(SystemExit) as raised:
        run_report(rejected)
    assert "--web-url" in str(raised.value)
    assert "路径" in str(raised.value)

    corrected = _parser().parse_args(
        [
            "eval-report",
            "--report",
            str(tmp_path / "report"),
            "--categories",
            str(categories),
            "--web-url",
            "https://analytics.example.test/",
        ]
    )
    assert run_report(corrected) == 0
    updated = json.loads((tmp_path / "report.json").read_text())
    assert updated["web_url"] == "https://analytics.example.test"
    assert (
        updated["questions"][0]["conversation_url"]
        == "https://analytics.example.test/chat/conv-good-1"
    )


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
    assert by_id["mixed"]["clarification_received"] is False
    assert by_id["mixed"]["clarification_followed_up"] is False
    assert by_id["clarify"]["result"] == "correct"
    assert by_id["clarify"]["clarified"] is True
    assert by_id["clarify"]["clarified_runs"] == 3
    assert by_id["clarify"]["clarification_received"] is True
    assert by_id["clarify"]["clarification_received_runs"] == 3
    assert by_id["clarify"]["clarification_followed_up"] is True
    assert by_id["clarify"]["clarification_followed_up_runs"] == 3
    assert all(
        run["clarification_received"] is True
        and run["clarification_followed_up"] is True
        for run in by_id["clarify"]["runs"]
    )
    ask_bodies = [
        body for path, body in service.asks if path == "/api/v1/conversations/ask"
    ]
    follow_ups = [body for body in ask_bodies if body.get("conversation_id")]
    assert len(follow_ups) == 3
    assert all("按订单口径" in body["question"] for body in follow_ups)

    metrics = report["metrics"]
    assert metrics["execution_total"] == 5
    assert metrics["execution_success"] == 5
    assert metrics["clarified"] == 1
    assert metrics["clarified_runs"] == 3
    assert metrics["clarification_received"] == 1
    assert metrics["clarification_received_runs"] == 3
    assert metrics["clarification_followed_up"] == 1
    assert metrics["clarification_followed_up_runs"] == 3
    assert metrics["no_sql"] == 1
    assert metrics["no_sql_runs"] == 1
    assert metrics["via_plan"] == 0
    assert metrics["via_plan_runs"] == 0
    assert metrics["condition_incomplete"] == 1
    assert metrics["condition_incomplete_runs"] == 1


def test_collect_unconfirmed_requires_skip_reference_before_any_request(
    service, tmp_path, monkeypatch
):
    reference = "SELECT reference FROM shop.orders WHERE status = 'paid'"
    service.reset({"unconfirmed": [GOOD]})
    path = _write_questions(
        tmp_path,
        [_question("u1", "unconfirmed", confirmed=False, reference_sql=reference)],
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
            "--collect-unconfirmed",
        ]
    )

    with pytest.raises(SystemExit) as raised:
        run_cli(args)

    message = str(raised.value)
    assert "--collect-unconfirmed" in message
    assert "--skip-reference" in message
    assert service.asks == []
    assert service.plans == {}
    assert service.executed == []
    assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "report.md").exists()


def test_collect_unconfirmed_keeps_runs_and_excludes_them_from_denominators(
    service, tmp_path, monkeypatch
):
    dev_reference = "SELECT reference_dev FROM shop.orders WHERE dt = '2025-01-01'"
    unconfirmed_reference = (
        "SELECT reference_unconfirmed FROM shop.orders WHERE status = 'paid'"
    )
    holdout_reference = "SELECT reference_holdout FROM shop.hidden"
    service.reset(
        {
            "dev confirmed": [GOOD, GOOD],
            "dev unconfirmed": [GOOD, BAD],
            "holdout confirmed": [GOOD],
            "holdout unconfirmed": [GOOD],
        }
    )
    path = _write_questions(
        tmp_path,
        [
            _question(
                "d1", "dev confirmed", category="选表", reference_sql=dev_reference
            ),
            _question(
                "u1",
                "dev unconfirmed",
                category="维度值筛选",
                confirmed=False,
                reference_sql=unconfirmed_reference,
            ),
            _question(
                "h1",
                "holdout unconfirmed",
                category="时间语义",
                split="holdout",
                confirmed=False,
                reference_sql=holdout_reference,
            ),
            _question(
                "h2",
                "holdout confirmed",
                category="多表 Join",
                split="holdout",
                reference_sql=holdout_reference,
            ),
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
            "--repeat",
            "3",
            "--collect-unconfirmed",
            "--skip-reference",
        ]
    )
    assert run_cli(args) == 0

    report = json.loads((tmp_path / "report.json").read_text())
    asked = [
        body["question"]
        for path, body in service.asks
        if path.endswith("/conversations/ask")
    ]
    assert asked == [
        "dev confirmed",
        "dev confirmed",
        "dev confirmed",
        "dev unconfirmed",
        "dev unconfirmed",
        "dev unconfirmed",
    ]
    planned = list(service.plans.values())
    assert dev_reference not in planned
    assert unconfirmed_reference not in planned
    assert holdout_reference not in planned
    assert dev_reference not in service.executed
    assert unconfirmed_reference not in service.executed
    assert holdout_reference not in service.executed

    assert report["collect_unconfirmed"] is True
    assert report["collected_unconfirmed"] == ["u1"]
    assert report["excluded_unconfirmed"] == ["h1"]
    assert [entry["id"] for entry in report["questions"]] == ["d1", "u1"]

    by_id = {entry["id"]: entry for entry in report["questions"]}
    confirmed = by_id["d1"]
    collected = by_id["u1"]
    assert collected["answer_confirmed"] is False
    assert collected["collect_only"] is True
    assert collected["result"] == "uncomparable"
    assert collected["uncomparable_reason"] == "answer_confirmed=false"
    assert collected["correct"] is False
    assert collected["equal_runs"] == 0
    assert collected["comparable_runs"] == 0
    assert collected["condition_incomplete"] is False
    assert collected["missing_condition_columns"] is None
    assert collected["reference_column_count"] is None
    assert collected["reference"]["status"] == "skipped"
    assert collected["run_count"] == 3
    assert collected["volatility"] == "unknown"
    assert collected["volatile"] is None
    assert collected["volatility_reason"] == "no_comparable_runs"
    assert confirmed["result"] == "uncomparable"
    assert confirmed["uncomparable_reason"] == "reference_skipped"
    assert confirmed["missing_condition_columns"] == []
    assert confirmed["volatility"] == "unknown"
    assert confirmed["volatile"] is None

    collected_runs = collected["runs"]
    assert len(collected_runs) == 3
    assert len({run["conversation_id"] for run in collected_runs}) == 3
    assert all(run["conversation_id"] for run in collected_runs)
    assert all(run["tool_calls"] for run in collected_runs)
    assert all(run["sql"] in {GOOD, BAD} for run in collected_runs)
    assert all(run["agent"]["status"] == "succeeded" for run in collected_runs)
    assert all(run["reference_status"] == "skipped" for run in collected_runs)
    assert all(run["comparison"] == "uncomparable" for run in collected_runs)
    assert all(run["missing_condition_columns"] is None for run in collected_runs)
    assert all(run["collect_only"] is True for run in collected_runs)

    metrics = report["metrics"]
    assert metrics["questions"] == 2
    assert metrics["runs"] == 6
    assert metrics["confirmed_questions"] == 1
    assert metrics["collected_only_questions"] == 1
    assert metrics["collected_only_runs"] == 3
    assert metrics["equal"] == 0
    assert metrics["comparable"] == 0
    assert metrics["consistency_rate"] is None
    assert metrics["correct"] == 0
    assert metrics["wrong"] == 0
    assert metrics["uncomparable"] == 2
    assert metrics["execution_total"] == 6
    assert metrics["execution_success"] == 6
    assert metrics["condition_incomplete"] == 0
    assert metrics["volatile"] == 0
    assert metrics["volatility_unknown"] == 2

    assert report["coverage"]["维度值筛选"]["collect_only"] == 1
    assert report["coverage"]["维度值筛选"]["uncomparable"] == 1
    assert report["coverage"]["维度值筛选"]["volatile"] == 0
    assert report["coverage"]["维度值筛选"]["volatility_unknown"] == 1
    assert report["coverage"]["选表"]["total"] == 1
    assert report["coverage"]["选表"]["volatility_unknown"] == 1
    question_set = report["question_set_coverage"]
    assert (
        question_set["total"],
        question_set["confirmed"],
        question_set["unconfirmed"],
    ) == (4, 2, 2)
    assert question_set["splits"] == {"dev": 2, "holdout": 2}
    reasons = {item["id"]: item["reason"] for item in report["excluded_uncomparable"]}
    assert reasons == {"d1": "reference_skipped", "u1": "answer_confirmed=false"}

    serialized = (tmp_path / "report.json").read_text()
    markdown = (tmp_path / "report.md").read_text()
    assert holdout_reference not in serialized
    assert holdout_reference not in markdown
    assert "仅采集" in markdown
    assert "answer_confirmed=false" in markdown
    assert "不可比原因：answer_confirmed=false" in markdown
    assert "其中仅采集题：1" in markdown
    assert "波动题：未知（无可比较结果）" in markdown
    assert "波动题：否" not in markdown

    service.reset({"holdout confirmed": [GOOD], "holdout unconfirmed": [GOOD]})
    holdout_args = _args(tmp_path, path, split="holdout", name="holdout-report")
    holdout_args.base_url = service.base_url
    holdout_args.collect_unconfirmed = True
    holdout_args.skip_reference = True
    assert run_cli(holdout_args) == 0
    holdout_asked = [
        body["question"]
        for path, body in service.asks
        if path.endswith("/conversations/ask")
    ]
    assert holdout_asked == ["holdout unconfirmed", "holdout confirmed"]
    assert dev_reference not in service.plans.values()
    assert unconfirmed_reference not in service.plans.values()
    holdout = json.loads((tmp_path / "holdout-report.json").read_text())
    assert [entry["id"] for entry in holdout["questions"]] == ["h1", "h2"]


def test_collect_unconfirmed_clarification_keeps_trace_without_caliber_follow_up(
    service, tmp_path, monkeypatch
):
    question_text = "unconfirmed clarify"
    caliber = "未审定口径-绝不应发送"
    service.reset({question_text: [GOOD, GOOD]}, clarify={question_text})
    path = _write_questions(
        tmp_path,
        [
            _question(
                "u1",
                question_text,
                category="口径歧义",
                confirmed=False,
                caliber=caliber,
            )
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, repeat=2, name="clarify-collect-report")
    args.base_url = service.base_url
    args.collect_unconfirmed = True
    args.skip_reference = True
    assert run_cli(args) == 0

    ask_bodies = [
        body for path, body in service.asks if path == "/api/v1/conversations/ask"
    ]
    assert len(ask_bodies) == 2
    assert all(body["question"] == question_text for body in ask_bodies)
    assert all(not body.get("conversation_id") for body in ask_bodies)
    serialized_requests = json.dumps(service.asks, ensure_ascii=False)
    assert caliber not in serialized_requests
    assert "请按以下口径回答并给出 SQL" not in serialized_requests

    report = json.loads((tmp_path / "clarify-collect-report.json").read_text())
    entry = report["questions"][0]
    assert entry["collect_only"] is True
    assert entry["result"] == "uncomparable"
    assert entry["correct"] is False
    assert entry["comparable_runs"] == 0
    assert entry["clarified"] is True
    assert entry["clarified_runs"] == 2
    assert entry["clarification_received"] is True
    assert entry["clarification_received_runs"] == 2
    assert entry["clarification_followed_up"] is False
    assert entry["clarification_followed_up_runs"] == 0
    assert entry["no_sql"] is True
    assert entry["no_sql_runs"] == 2
    assert entry["sql"] is None
    assert entry["agent_sql"] is False

    runs = entry["runs"]
    assert len(runs) == 2
    assert all(run["clarification_received"] is True for run in runs)
    assert all(run["clarification_followed_up"] is False for run in runs)
    assert all(run["agent"]["status"] == "no_sql" for run in runs)
    assert all(run["sql"] is None and run["agent_sql"] is False for run in runs)
    assert all(run["tool_calls"] for run in runs)
    conversation_ids = [run["conversation_id"] for run in runs]
    assert len(conversation_ids) == len(set(conversation_ids)) == 2

    metrics = report["metrics"]
    assert metrics["clarified"] == 1
    assert metrics["clarified_runs"] == 2
    assert metrics["clarification_received"] == 1
    assert metrics["clarification_received_runs"] == 2
    assert metrics["clarification_followed_up"] == 0
    assert metrics["clarification_followed_up_runs"] == 0
    assert metrics["no_sql"] == 1
    assert metrics["no_sql_runs"] == 2

    markdown = (tmp_path / "clarify-collect-report.md").read_text()
    assert "收到澄清：是（2/2 遍）" in markdown
    assert "自动追问：否（0/2 遍）" in markdown
    assert "收到澄清后不自动发送题集口径" in markdown
    assert caliber not in markdown


def test_eval_report_separates_evidence_backed_observation_for_collected_unconfirmed(
    service, tmp_path, monkeypatch
):
    service.reset({"confirmed": [GOOD], "unconfirmed": [GOOD]})
    path = _write_questions(
        tmp_path,
        [
            _question("d1", "confirmed", category="选表"),
            _question("u1", "unconfirmed", category="维度值筛选", confirmed=False),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, repeat=3, name="collect-report")
    args.base_url = service.base_url
    args.collect_unconfirmed = True
    args.skip_reference = True
    assert run_cli(args) == 0

    evidence = (
        "第 2 遍 agent status=succeeded，工具调用显示生成了不带分区条件的 SQL；"
        "依据轨迹人工复核，未使用参考 SQL"
    )
    categories = tmp_path / "categories.yaml"
    categories.write_text(
        yaml.safe_dump(
            [
                {"id": "d1", "failure_category": "选错表"},
                {
                    "id": "u1",
                    "failure_category": "口径错",
                    "evidence": evidence,
                },
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    report_args = _parser().parse_args(
        [
            "eval-report",
            "--report",
            str(tmp_path / "collect-report"),
            "--categories",
            str(categories),
        ]
    )
    assert run_report(report_args) == 0

    report = json.loads((tmp_path / "collect-report.json").read_text())
    by_id = {entry["id"]: entry for entry in report["questions"]}
    confirmed = by_id["d1"]
    collected = by_id["u1"]

    assert confirmed["failure_category"] == "选错表"
    assert confirmed["observed_failure_category"] is None
    assert confirmed["observed_failure_evidence"] is None
    assert collected["failure_category"] is None
    assert collected["observed_failure_category"] == "口径错"
    assert collected["observed_failure_evidence"] == evidence
    assert report["failure_categories"] == {"选错表": 1}
    assert report["observed_failure_categories"] == {"口径错": 1}

    metrics = report["metrics"]
    assert metrics["confirmed_questions"] == 1
    assert metrics["collected_only_questions"] == 1
    assert metrics["classified"] == 1
    assert metrics["unclassified"] == 0
    assert metrics["equal"] == 0
    assert metrics["correct"] == 0
    assert metrics["wrong"] == 0
    assert metrics["comparable"] == 0
    assert metrics["consistency_rate"] is None
    assert metrics["volatile"] == 0
    assert metrics["volatility_unknown"] == 2

    confirmed_coverage = report["coverage"]["选表"]
    collected_coverage = report["coverage"]["维度值筛选"]
    assert confirmed_coverage["failure_categories"] == {"选错表": 1}
    assert confirmed_coverage["observed_failure_categories"] == {}
    assert collected_coverage["failure_categories"] == {}
    assert collected_coverage["observed_failure_categories"] == {"口径错": 1}
    assert collected_coverage["volatility_unknown"] == 1

    markdown = (tmp_path / "collect-report.md").read_text()
    assert "仅采集题人工观察（答案未验证）" in markdown
    assert "不可据此宣称答案正确或错误" in markdown
    assert "不进入正确率、结果一致率、可比较题或已确认题分母" in markdown
    assert "人工观察分类：口径错" in markdown
    assert f"人工观察证据：{evidence}" in markdown
    assert "failure_category：不适用" in markdown
    assert "波动题：未知（无可比较结果）" in markdown
    assert "波动题：否" not in markdown

    categories.write_text(yaml.safe_dump({"u1": None}), encoding="utf-8")
    assert run_report(report_args) == 0
    report = json.loads((tmp_path / "collect-report.json").read_text())
    cleared = {entry["id"]: entry for entry in report["questions"]}["u1"]
    assert cleared["observed_failure_category"] is None
    assert cleared["observed_failure_evidence"] is None
    assert report["observed_failure_categories"] == {}
    assert report["failure_categories"] == {"选错表": 1}

    categories.write_text(yaml.safe_dump({"u1": "口径错"}), encoding="utf-8")
    assert run_report(report_args) == 0
    report = json.loads((tmp_path / "collect-report.json").read_text())
    observed_only = {entry["id"]: entry for entry in report["questions"]}["u1"]
    assert observed_only["observed_failure_category"] == "口径错"
    assert observed_only["observed_failure_evidence"] is None
    assert report["observed_failure_categories"] == {"口径错": 1}


def test_clarification_exploratory_sql_is_not_executed_and_follow_up_completes(
    service, tmp_path, monkeypatch
):
    question_text = "synthetic clarify explore"
    service.reset(
        {question_text: [FINAL]},
        clarify={question_text},
        clarify_exploratory={question_text: EXPLORATORY},
    )
    path = _write_questions(
        tmp_path,
        [_question("q1", question_text, category="口径歧义")],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path)
    args.base_url = service.base_url
    assert run_cli(args) == 0

    assert EXPLORATORY not in service.executed
    assert FINAL in service.executed
    ask_bodies = [
        body for path, body in service.asks if path == "/api/v1/conversations/ask"
    ]
    assert len(ask_bodies) == 2
    assert ask_bodies[0]["question"] == question_text
    assert ask_bodies[1]["conversation_id"]

    report = json.loads((tmp_path / "report.json").read_text())
    entry = report["questions"][0]
    assert entry["sql"] == FINAL
    assert entry["actual_sql"] == FINAL
    assert entry["sql_source"] == "done"
    assert entry["final_query"] is True
    assert entry["clarification_received"] is True
    assert entry["clarification_followed_up"] is True
    run = entry["runs"][0]
    assert run["sql"] == FINAL
    assert run["sql_source"] == "done"
    assert run["final_query"] is True
    assert run["agent"]["status"] == "succeeded"
    assert report["metrics"]["execution_total"] == 1
    assert report["metrics"]["execution_success"] == 1
    assert "最终业务查询：是（SQL 来源：done）" in (tmp_path / "report.md").read_text()


def test_collect_only_clarification_exploratory_sql_is_not_executed(
    service, tmp_path, monkeypatch
):
    question_text = "synthetic collect clarify explore"
    service.reset(
        {},
        clarify={question_text},
        clarify_exploratory={question_text: EXPLORATORY},
    )
    path = _write_questions(
        tmp_path,
        [
            _question(
                "u1",
                question_text,
                category="口径歧义",
                confirmed=False,
            )
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, name="collect-explore-report")
    args.base_url = service.base_url
    args.collect_unconfirmed = True
    args.skip_reference = True
    assert run_cli(args) == 0

    assert service.executed == []
    ask_bodies = [
        body for path, body in service.asks if body.get("question") == question_text
    ]
    assert len(ask_bodies) == 1

    report = json.loads((tmp_path / "collect-explore-report.json").read_text())
    entry = report["questions"][0]
    assert entry["sql"] is None
    assert entry["agent_sql"] is False
    assert entry["sql_source"] is None
    assert entry["final_query"] is False
    assert entry["clarification_received"] is True
    assert entry["clarification_followed_up"] is False
    run = entry["runs"][0]
    assert run["agent"]["status"] == "no_sql"
    assert run["sql"] is None
    assert run["sql_source"] is None
    assert run["final_query"] is False


def test_stream_error_after_done_does_not_execute_residual_sql(
    service, tmp_path, monkeypatch
):
    question_text = "synthetic error after sql"
    service.reset(
        {question_text: [GOOD]},
        stream_errors={question_text},
    )
    path = _write_questions(
        tmp_path,
        [_question("q1", question_text)],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, name="error-report")
    args.base_url = service.base_url
    args.skip_reference = True
    assert run_cli(args) == 0

    assert service.executed == []
    report = json.loads((tmp_path / "error-report.json").read_text())
    entry = report["questions"][0]
    assert entry["sql"] is None
    assert entry["agent_sql"] is False
    assert entry["sql_source"] is None
    assert entry["final_query"] is False
    assert entry["agent"]["status"] == "no_sql"
    assert entry["runs"][0]["sql"] is None


def test_ask_discards_sql_when_stream_raises():
    import httpx

    from semantica_dataagent.eval_exec import _ask

    class Response:
        is_success = True

        def iter_lines(self):
            yield "event: done"
            yield 'data: {"sql": [{"sql": "SELECT residual FROM shop.orders"}]}'
            raise httpx.ReadError("synthetic connection loss")

    class Stream:
        def __enter__(self):
            return Response()

        def __exit__(self, *exc):
            return False

    class Client:
        def stream(self, method, path, json):
            return Stream()

    result = _ask(Client(), {"question": "synthetic stream error"})
    assert result["sql"] is None
    assert result["sql_source"] is None
    assert result["final_query"] is False
