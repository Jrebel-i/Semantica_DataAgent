from semantica_dataagent import eval as evaluation


def test_execution_results_ignore_row_order_and_numeric_representation():
    compare = getattr(evaluation, "compare_results", lambda *args: None)
    left = {"columns": ["a"], "rows": [[1], [2]]}
    right = {"columns": ["other"], "rows": [["2.0000001"], ["1"]]}
    assert compare(left, right) == "equal"
