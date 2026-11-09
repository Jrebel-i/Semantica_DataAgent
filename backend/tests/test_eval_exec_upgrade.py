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


class FakeQuestionService:
    """Small fake ask + query service; no model or data source is involved."""

    def __init__(self):
        self.asks = []
        self.executed = []
        self.plans = {}
        self.counts = Counter()
        self.responses = {}
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def reset(self, responses=None):
        self.asks.clear()
        self.executed.clear()
        self.plans.clear()
        self.counts.clear()
        self.responses = dict(responses or {})

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
                    service.counts[question] += 1
                    run_number = service.counts[question]
                    conversation_id = body.get("conversation_id") or (
                        f"conv-{question}-{run_number}"
                    )
                    choices = service.responses.get(question) or [GOOD]
                    sql = choices[min(run_number - 1, len(choices) - 1)]
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
                            ("done", {"text": "已生成 SQL", "sql": [{"sql": sql}]}),
                        ]
                    )
                elif self.path == "/api/v1/queries/plan":
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
                    self._json(
                        200,
                        {
                            "columns": ["value"],
                            "rows": rows,
                            "row_count": len(rows),
                        },
                    )
                else:
                    self._json(404, {"detail": "not found"})

        return Handler


def _question(question_id, question, *, category="选表", split="dev", confirmed=True):
    row = {
        "id": question_id,
        "question": question,
        "expected_tables": ["shop.orders"],
        "expected_fields": ["id"],
        "caliber": "按订单口径",
        "reference_sql": GOOD,
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
    args = _args(tmp_path, path)
    args.base_url = service.base_url
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
    assert entry["correct"] is True
    assert entry["volatile"] is False
    assert entry["failure_category"] is None
    run = entry["runs"][0]
    assert run["sql"] == GOOD
    assert run["reference_sql"] == GOOD
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
