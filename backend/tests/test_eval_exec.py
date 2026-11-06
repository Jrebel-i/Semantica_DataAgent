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
    assert compare(result, {"columns": ["y"], "rows": [[1], [None], [2]]}) == "different"
    assert compare(result, {"columns": ["y"], "rows": [], "truncated": True}) == "uncomparable"
    assert compare({"columns": ["x"], "rows": []}, {"columns": [], "rows": []}) == "different"
    assert compare({"columns": ["x"], "rows": []}, {"columns": ["y"], "rows": []}) == "equal"


def test_exec_cli_is_available():
    from semantica_dataagent.cli import _parser

    args = _parser().parse_args(["eval-exec", "--questions", "questions.yaml", "--report", "report", "--base-url", "http://localhost", "--token", "EVAL_TOKEN", "--agent", "pi", "--provider-id", "p", "--model", "m"])
    assert args.max_queries == 40
