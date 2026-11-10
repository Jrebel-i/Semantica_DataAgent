"""#235 end-to-end check: split and repeat evaluation with a fake ask service."""

from __future__ import annotations

import json
import os
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import yaml

GOOD = "SELECT id FROM shop.orders WHERE dt = '2025-01-01'"
BAD = "SELECT id FROM shop.orders"
BROKEN = "SELECT broken FROM shop.orders"
HOLDOUT = "SELECT holdout_answer FROM shop.hidden"


class _FakeService:
    def __init__(self):
        self.asks = []
        self.plans = {}
        self.counts = Counter()
        self.responses = {
            "stable": [GOOD, GOOD, GOOD],
            "volatile": [GOOD, GOOD, BAD],
            "late": [BAD, GOOD, GOOD],
            "early": [GOOD, BAD, BAD],
            "always bad": [BAD, BAD, BAD],
            "reference broken": [GOOD, GOOD, GOOD],
            "holdout": [GOOD],
        }
        self.fail_sqls = {BROKEN}
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

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
                    number = service.counts[question]
                    choices = service.responses.get(question) or [GOOD]
                    sql = choices[min(number - 1, len(choices) - 1)]
                    self._sse(
                        [
                            (
                                "conversation",
                                {"conversation_id": f"conv-{question}-{number}"},
                            ),
                            (
                                "tool_start",
                                {"name": "search_knowledge", "summary": "检索"},
                            ),
                            (
                                "tool_start",
                                {"name": "propose_query", "summary": "生成 SQL"},
                            ),
                            ("done", {"text": "", "sql": [{"sql": sql}]}),
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
                    sql = service.plans[self.path.split("/")[4]]
                    rows = [[1]] if sql == GOOD else [[1], [2]]
                    self._json(
                        200,
                        {"columns": ["value"], "rows": rows, "row_count": len(rows)},
                    )
                else:
                    self._json(404, {"detail": "not found"})

        return Handler


def _row(
    question_id,
    question,
    *,
    category,
    split="dev",
    confirmed=True,
    reference_sql=GOOD,
):
    return {
        "id": question_id,
        "question": question,
        "expected_tables": ["shop.orders"],
        "expected_fields": ["id"],
        "caliber": "订单口径",
        "reference_sql": reference_sql,
        "category": category,
        "split": split,
        "answer_confirmed": confirmed,
    }


def check_eval_tool_upgrade(ctx):
    from semantica_dataagent.cli import main

    work = Path(ctx.work_dir) / "issue-235"
    work.mkdir(parents=True, exist_ok=True)
    questions = work / "questions.yaml"
    questions.write_text(
        yaml.safe_dump(
            [
                _row("q3", "stable", category="选表"),
                _row("q2", "volatile", category="口径"),
                _row("qlate", "late", category="选表"),
                _row("qearly", "early", category="选表"),
                _row("qbad", "always bad", category="多表 Join"),
                _row(
                    "qref",
                    "reference broken",
                    category="选表",
                    reference_sql=BROKEN,
                ),
                _row(
                    "h1",
                    "holdout",
                    category="时间语义",
                    split="holdout",
                    reference_sql=HOLDOUT,
                ),
                _row(
                    "u1",
                    "unconfirmed",
                    category="维度值筛选",
                    confirmed=False,
                ),
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    service = _FakeService()
    old = os.environ.get("SDA_ISSUE_235_E2E_TOKEN")
    os.environ["SDA_ISSUE_235_E2E_TOKEN"] = "issue-235-e2e-secret"
    try:
        code = main(
            [
                "eval-exec",
                "--questions",
                str(questions),
                "--report",
                str(work / "report"),
                "--base-url",
                service.base_url,
                "--web-url",
                "https://eval.example.test",
                "--token",
                "SDA_ISSUE_235_E2E_TOKEN",
                "--agent",
                "pi",
                "--provider-id",
                "fake",
                "--model",
                "fake",
                "--split",
                "dev",
                "--repeat",
                "3",
            ]
        )
        ctx.require(code == 0, f"eval-exec returned {code}")
        report = json.loads((work / "report.json").read_text(encoding="utf-8"))
        ctx.require(
            [entry["id"] for entry in report["questions"]]
            == ["q3", "q2", "qlate", "qearly", "qbad", "qref"],
            f"default split ran unexpected questions: {report['questions']}",
        )
        ctx.require(
            report["excluded_unconfirmed"] == ["u1"],
            f"unconfirmed question was not listed: {report['excluded_unconfirmed']}",
        )
        by_id = {entry["id"]: entry for entry in report["questions"]}
        ctx.require(by_id["q3"]["result"] == "correct", "3/3 should be correct")
        ctx.require(not by_id["q3"]["volatile"], "3/3 should not be volatile")
        ctx.require(by_id["q2"]["result"] == "correct", "2/3 should be correct")
        ctx.require(by_id["q2"]["volatile"], "2/3 should be volatile")
        ctx.require(by_id["q2"]["equal_runs"] == 2, "expected two equal runs")
        ctx.require(by_id["qlate"]["result"] == "correct", "late 2/3 should be correct")
        ctx.require(
            by_id["qlate"]["comparison"] == "equal", "late comparison should be equal"
        )
        ctx.require(
            by_id["qlate"]["sql"] == GOOD
            and by_id["qlate"]["conversation_id"] == "conv-late-2",
            "displayed SQL must come from the majority run",
        )
        ctx.require(by_id["qearly"]["result"] == "wrong", "early 1/3 should be wrong")
        ctx.require(
            by_id["qearly"]["comparison"] == "different"
            and by_id["qearly"]["sql"] == BAD
            and by_id["qearly"]["conversation_id"] == "conv-early-2",
            "wrong question must display the failing majority SQL",
        )
        ctx.require(by_id["qbad"]["result"] == "wrong", "all-bad should be wrong")
        ctx.require(
            by_id["qref"]["result"] == "uncomparable"
            and by_id["qref"]["reference"]["status"] == "failed",
            "reference failure must be uncomparable",
        )
        ctx.require(
            [item["id"] for item in report["excluded_uncomparable"]] == ["qref"],
            f"uncomparable list wrong: {report['excluded_uncomparable']}",
        )
        metrics = report["metrics"]
        ctx.require(
            metrics["equal"] == 3
            and metrics["comparable"] == 5
            and metrics["consistency_rate"] == 0.6,
            f"majority metrics wrong: {metrics}",
        )
        ctx.require(
            metrics["correct"] == 3
            and metrics["wrong"] == 2
            and metrics["uncomparable"] == 1
            and metrics["reference_failures"] == 1,
            f"verdict metrics wrong: {metrics}",
        )
        ctx.require(
            "majority_consistency_rate" not in metrics,
            "legacy majority metric should be removed",
        )
        ctx.require(
            report["coverage_gaps"] == ["口径歧义", "合法空结果"],
            f"coverage gaps wrong: {report['coverage_gaps']}",
        )
        ctx.require(
            report["confirmed_coverage_gaps"]
            == ["维度值筛选", "口径歧义", "合法空结果"],
            f"confirmed coverage gaps wrong: {report['confirmed_coverage_gaps']}",
        )
        ctx.require(
            report["scope_coverage_gaps"]
            == ["维度值筛选", "时间语义", "口径歧义", "合法空结果"],
            f"scope coverage gaps wrong: {report['scope_coverage_gaps']}",
        )
        ctx.require(
            report["unconfirmed_only_categories"] == ["维度值筛选"],
            f"unconfirmed-only categories wrong: {report['unconfirmed_only_categories']}",
        )
        ctx.require(
            report["question_set_coverage"]["includes_unconfirmed"] is True
            and report["question_set_coverage"]["unconfirmed"] == 1,
            f"question-set coverage did not label unconfirmed questions: {report['question_set_coverage']}",
        )
        expected_link = "https://eval.example.test/chat/conv-stable-1"
        ctx.require(
            by_id["q3"]["conversation_url"] == expected_link
            and by_id["q3"]["runs"][0]["conversation_url"] == expected_link,
            f"conversation link missing or wrong: {by_id['q3'].get('conversation_url')}",
        )
        ctx.require(
            report["coverage"]["多表 Join"]["total"] == 1
            and report["coverage"]["多表 Join"]["correct"] == 0,
            f"all-wrong category must not be a gap: {report['coverage']}",
        )
        ctx.require(
            len({run["conversation_id"] for run in by_id["q2"]["runs"]}) == 3,
            "each repeat must use a new conversation",
        )
        ctx.require(
            all(
                [call["name"] for call in run["tool_calls"]]
                == ["search_knowledge", "propose_query"]
                for entry in report["questions"]
                for run in entry["runs"]
            ),
            "tool call sequence missing from report",
        )
        ctx.require(
            "holdout"
            not in {
                body["question"] for _, body in service.asks if body.get("question")
            },
            "holdout question ran without --split holdout",
        )
        code = main(
            [
                "eval-exec",
                "--questions",
                str(questions),
                "--report",
                str(work / "override"),
                "--base-url",
                service.base_url,
                "--token",
                "SDA_ISSUE_235_E2E_TOKEN",
                "--agent",
                "pi",
                "--provider-id",
                "fake",
                "--model",
                "fake",
                "--only",
                "q3",
                "--expected-categories",
                "选表,时间语义",
            ]
        )
        ctx.require(code == 0, f"eval-exec override returned {code}")
        override = json.loads((work / "override.json").read_text(encoding="utf-8"))
        ctx.require(
            override["expected_categories"] == ["选表", "时间语义"]
            and override["coverage_gaps"] == []
            and override["scope_coverage_gaps"] == ["时间语义"],
            f"expected categories override wrong: {override.get('coverage_gaps')}",
        )
        categories = work / "categories.yaml"
        categories.write_text(
            yaml.safe_dump({"q3": "选错表", "q2": "口径错"}, allow_unicode=True),
            encoding="utf-8",
        )
        code = main(
            [
                "eval-report",
                "--report",
                str(work / "report"),
                "--categories",
                str(categories),
            ]
        )
        ctx.require(code == 0, f"eval-report returned {code}")
        merged = json.loads((work / "report.json").read_text(encoding="utf-8"))
        ctx.require(
            merged["failure_categories"] == {"选错表": 1, "口径错": 1},
            f"failure summary wrong: {merged['failure_categories']}",
        )

        split_questions = work / "split.yaml"
        split_questions.write_text(
            yaml.safe_dump(
                [
                    _row(
                        f"s{index}",
                        f"split {index}",
                        category="A" if index < 5 else "B",
                    )
                    for index in range(10)
                ],
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        for _ in range(2):
            code = main(
                [
                    "eval-split",
                    "--questions",
                    str(split_questions),
                    "--holdout-size",
                    "4",
                    "--seed",
                    "17",
                ]
            )
            ctx.require(code == 0, f"eval-split returned {code}")
            payload = yaml.safe_load(split_questions.read_text(encoding="utf-8"))
            counts = Counter(row["split"] for row in payload)
            ctx.require(
                counts == {"dev": 6, "holdout": 4},
                f"split counts wrong: {counts}",
            )
        ctx.require(
            "issue-235-e2e-secret"
            not in (work / "report.md").read_text(encoding="utf-8"),
            "token leaked into the report",
        )
        markdown = (work / "report.md").read_text(encoding="utf-8")
        ctx.require(
            "不可比题目" in markdown
            and "覆盖缺口" in markdown
            and "本次评测覆盖" in markdown
            and "qref" in markdown,
            "report markdown lacks rework sections",
        )
        ctx.require(
            expected_link in markdown,
            "report markdown lacks the external conversation link",
        )
        ctx.require(
            HOLDOUT not in markdown
            and HOLDOUT not in (work / "report.json").read_text(encoding="utf-8"),
            "holdout reference SQL leaked into the development report",
        )
        return {
            "questions": [entry["id"] for entry in report["questions"]],
            "excluded": report["excluded_unconfirmed"],
            "uncomparable": [item["id"] for item in report["excluded_uncomparable"]],
            "metrics": metrics,
            "coverage_gaps": report["coverage_gaps"],
            "failure_categories": merged["failure_categories"],
            "split_counts": dict(counts),
        }
    finally:
        service.close()
        if old is None:
            os.environ.pop("SDA_ISSUE_235_E2E_TOKEN", None)
        else:
            os.environ["SDA_ISSUE_235_E2E_TOKEN"] = old
