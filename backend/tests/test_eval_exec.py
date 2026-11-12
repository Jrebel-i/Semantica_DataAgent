from semantica_dataagent import eval as evaluation


def test_execution_results_ignore_row_order_and_numeric_representation():
    compare = getattr(evaluation, "compare_results", lambda *args: None)
    left = {"columns": ["a"], "rows": [[1], [2]]}
    right = {"columns": ["other"], "rows": [["2.0000001"], ["1"]]}
    assert compare(left, right) == "equal"


def test_compare_results_handles_truncation_nulls_and_duplicates():
    compare = evaluation.compare_results
    result = {"columns": ["x"], "rows": [[None], [1], [1]]}
    assert compare(result, {"columns": ["y"], "rows": [[1], [None], [1]]}) == "equal"
    assert (
        compare(result, {"columns": ["y"], "rows": [[1], [None], [2]]}) == "different"
    )
    assert (
        compare(result, {"columns": ["y"], "rows": [], "truncated": True})
        == "uncomparable"
    )
    assert (
        compare({"columns": ["x"], "rows": []}, {"columns": [], "rows": []})
        == "different"
    )
    assert (
        compare({"columns": ["x"], "rows": []}, {"columns": ["y"], "rows": []})
        == "equal"
    )


def test_exec_cli_is_available():
    from semantica_dataagent.cli import _parser

    args = _parser().parse_args(
        [
            "eval-exec",
            "--questions",
            "questions.yaml",
            "--report",
            "report",
            "--base-url",
            "http://localhost",
            "--token",
            "EVAL_TOKEN",
            "--agent",
            "pi",
            "--provider-id",
            "p",
            "--model",
            "m",
            "--project",
            "docs",
        ]
    )
    assert args.max_queries == 40
    assert args.project == "docs"


def test_condition_columns_and_missing_condition_detection():
    from semantica_dataagent.eval_exec import where_columns

    reference = (
        "SELECT COUNT(*) FROM shop.orders WHERE dt >= '2025-01-01' AND status = 'paid'"
    )
    agent = "SELECT COUNT(*) FROM shop.orders WHERE dt >= '2025-01-01'"

    assert where_columns(reference, "hive") == {"dt", "status"}
    assert where_columns(agent, "hive") == {"dt"}


def test_comparison_handles_large_duplicate_multiset():
    result = {"columns": ["x"], "rows": [[1]] * 1100}
    assert evaluation.compare_results(result, result) == "equal"


def test_numeric_tolerance_null_and_column_positions():
    def result(*rows):
        return {"columns": ["a", "b"], "rows": list(rows)}

    assert (
        evaluation.compare_results(result([100, None]), result(["100.00009", None]))
        == "equal"
    )
    assert (
        evaluation.compare_results(result([100, None]), result(["100.0002", None]))
        == "different"
    )
    assert (
        evaluation.compare_results(result([1, None]), result([None, 1])) == "different"
    )
    assert evaluation.compare_results(result([None, 1]), result(["", 1])) == "different"


def test_fake_agent_direct_clarification_and_query_plan(monkeypatch, tmp_path):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    import yaml

    from semantica_dataagent.eval_exec import run_cli

    requests = []
    plans = {}
    executed_sqls = []
    detail = "缺少分区条件：" + "x" * 300
    base = "SELECT id FROM shop.orders WHERE dt = '2025-01-01'"

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
            requests.append((self.path, body))
            if self.path == "/api/v1/conversations/ask":
                if body.get("conversation_id"):
                    self._sse(
                        [
                            (
                                "done",
                                {
                                    "sql": [
                                        {
                                            "sql": "SELECT clarified FROM "
                                            "shop.orders WHERE dt = '2025-01-01'"
                                        }
                                    ]
                                },
                            )
                        ]
                    )
                    return
                question = body["question"]
                if question == "clarify":
                    self._sse(
                        [
                            ("conversation", {"conversation_id": "conv-clarify"}),
                            (
                                "clarification",
                                {
                                    "questions": [
                                        {
                                            "question": "用哪个口径？",
                                            "options": [{"label": "销售额"}],
                                        }
                                    ]
                                },
                            ),
                            ("done", {"text": "", "sql": []}),
                        ]
                    )
                elif question == "plan":
                    self._sse(
                        [
                            ("conversation", {"conversation_id": "conv-plan"}),
                            (
                                "query_plan",
                                {
                                    "id": "agent-plan",
                                    "sql": "SELECT planned FROM "
                                    "shop.orders WHERE dt = '2025-01-01'",
                                },
                            ),
                        ]
                    )
                elif question == "bad":
                    self._sse(
                        [
                            ("conversation", {"conversation_id": "conv-bad"}),
                            (
                                "done",
                                {
                                    "sql": [
                                        {
                                            "sql": "SELECT bad FROM "
                                            "shop.orders WHERE dt = '2025-01-01'"
                                        }
                                    ]
                                },
                            ),
                        ]
                    )
                elif question == "drop":
                    self._sse(
                        [
                            ("conversation", {"conversation_id": "conv-drop"}),
                            (
                                "done",
                                {
                                    "sql": [
                                        {
                                            "sql": "SELECT dropped FROM "
                                            "shop.orders WHERE dt = '2025-01-01'"
                                        }
                                    ]
                                },
                            ),
                        ]
                    )
                else:
                    self._sse(
                        [
                            ("conversation", {"conversation_id": "conv-direct"}),
                            (
                                "done",
                                {
                                    "sql": [
                                        {
                                            "sql": "SELECT direct FROM "
                                            "shop.orders WHERE dt = '2025-01-01'"
                                        }
                                    ]
                                },
                            ),
                        ]
                    )
            elif self.path == "/api/v1/queries/plan":
                if body["sql"].startswith("SELECT bad"):
                    self._json(422, {"detail": detail})
                    return
                query_id = f"query-{len(plans)}"
                plans[query_id] = body["sql"]
                self._json(200, {"id": query_id, "execution_limit": 10})
            elif self.path.startswith("/api/v1/queries/") and self.path.endswith(
                "/execute"
            ):
                query_id = self.path.split("/")[4]
                assert query_id in plans
                executed_sqls.append(plans[query_id])
                self._json(
                    200,
                    {
                        "columns": ["value"],
                        "rows": [[1]],
                        "row_count": 1,
                    },
                )
            else:
                self._json(404, {"detail": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        questions = [
            {
                "id": "r0",
                "question": "direct",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["id"],
                "caliber": "直接口径",
                "reference_sql": base,
            },
            {
                "id": "r1",
                "question": "clarify",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["id"],
                "caliber": "按业务审定口径",
                "reference_sql": base,
            },
            {
                "id": "r2",
                "question": "plan",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["id"],
                "caliber": "计划口径",
                "reference_sql": base,
            },
            {
                "id": "r3",
                "question": "bad",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["id"],
                "caliber": "失败口径",
                "reference_sql": "SELECT bad_reference FROM shop.orders "
                "WHERE dt = '2025-01-01'",
            },
            {
                "id": "r4",
                "question": "drop",
                "expected_tables": ["shop.orders"],
                "expected_fields": ["id"],
                "caliber": "丢掉状态条件的口径",
                "reference_sql": "SELECT id FROM shop.orders "
                "WHERE dt = '2025-01-01' AND status = 'paid'",
            },
        ]
        path = tmp_path / "questions.yaml"
        path.write_text(yaml.safe_dump(questions), encoding="utf-8")
        args = type(
            "Args",
            (),
            {
                "questions": path,
                "report": tmp_path / "report",
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "token": "EVAL_EXEC_TOKEN",
                "only": None,
                "max_queries": 40,
                "skip_reference": False,
                "agent": "pi",
                "provider_id": "fake",
                "model": "fake",
                "project": "docs",
            },
        )()
        monkeypatch.setenv("EVAL_EXEC_TOKEN", "issue-121-secret")
        assert run_cli(args) == 0

        raw = (tmp_path / "report.json").read_text()
        report = json.loads(raw)
        metrics = report["metrics"]
        entries = report["questions"]
        assert metrics["clarified"] == 1
        assert metrics["via_plan"] == 1
        assert metrics["no_sql"] == 0
        assert metrics["execution_total"] == 5
        assert metrics["execution_success"] == 4
        assert metrics["condition_incomplete"] == 1
        assert entries[0]["clarified"] is False
        assert entries[0]["sql_source"] == "done"
        assert entries[0]["final_query"] is True
        assert entries[1]["clarified"] is True
        assert entries[1]["sql_source"] == "done"
        assert entries[1]["final_query"] is True
        assert entries[2]["via_plan"] is True
        assert entries[2]["sql_source"] == "query_plan"
        assert entries[2]["final_query"] is True
        assert entries[2]["clarified"] is False
        assert [entry["comparison"] for entry in entries[:3]] == ["equal"] * 3
        follow_up = next(
            body
            for path, body in requests
            if path == "/api/v1/conversations/ask" and body.get("conversation_id")
        )
        assert follow_up["conversation_id"] == "conv-clarify"
        assert report["project"] == "docs"
        assert all(
            body.get("project") == "docs"
            for path, body in requests
            if path == "/api/v1/conversations/ask"
        )
        assert "请按以下口径回答并给出 SQL" in follow_up["question"]
        assert "按业务审定口径" in follow_up["question"]
        assert not any(
            path.startswith("/api/v1/conversations/")
            and path != "/api/v1/conversations/ask"
            for path, _ in requests
        ), "evaluator called the conversation confirmation endpoint"
        assert entries[3]["agent"]["reason"] == "校验被拒"
        assert entries[3]["agent"]["detail"] == detail[:200]
        assert entries[3]["detail"] == detail[:200]
        assert entries[3]["reference"]["reason"] == "校验被拒"
        assert entries[3]["reference_detail"] == detail[:200]
        assert entries[0]["missing_condition_columns"] == []
        assert entries[0]["agent_column_count"] == 1
        assert entries[0]["reference_column_count"] == 1
        assert entries[4]["missing_condition_columns"] == ["status"]
        assert entries[4]["comparison"] == "equal"
        assert entries[4]["agent_column_count"] == 1
        assert entries[4]["reference_column_count"] == 1
        planned = "SELECT planned FROM shop.orders WHERE dt = '2025-01-01'"
        assert planned in executed_sqls
        markdown = (tmp_path / "report.md").read_text()
        assert detail[:200] in markdown
        assert "缺失条件列" in markdown
        assert "status" in markdown
        assert "issue-121-secret" not in raw
        assert "issue-121-secret" not in markdown
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
