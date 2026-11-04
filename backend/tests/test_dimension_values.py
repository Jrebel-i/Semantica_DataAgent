from semantica_dataagent.canonical import Record
from semantica_dataagent.dimension_stage import prepare


def test_normalization_limits_and_row_pairing():
    records = [
        Record("dimension_value", x)
        for x in [
            {
                "database": "shop",
                "table": "os",
                "field": "os_type",
                "value": " android ",
                "row_id": "1",
            },
            {
                "database": "shop",
                "table": "os",
                "field": "os_name",
                "value": "安卓",
                "row_id": "1",
            },
            {
                "database": "shop",
                "table": "os",
                "field": "os_type",
                "value": "android",
                "row_id": "2",
            },
            {
                "database": "shop",
                "table": "os",
                "field": "os_name",
                "value": "机器人",
                "row_id": "2",
            },
            {
                "database": "shop",
                "table": "os",
                "field": "os_name",
                "value": "其他",
                "row_id": "3",
            },
        ]
    ]
    values, statuses = prepare(records, 2)
    code = [v for v in values if v["field"] == "os_type"]
    assert {v["value"] for v in code} == {" android ", "android"}
    assert all(v["normalized"] == "android" for v in code)
    assert any("安卓" in v["aliases"] for v in code)
    assert not any(v["field"] == "os_name" for v in values)
    assert next(s for s in statuses if s["field"] == "os_name")["too_many"]
