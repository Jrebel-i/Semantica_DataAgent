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


def test_write_batches_preserve_values_and_field_statuses(monkeypatch):
    from semantica_dataagent import dimension_stage

    class Storage:
        is_incremental = False
        build_id = 7

        def __init__(self):
            self._settings = {}
            self.rows = {"statuses": [], "values": []}

        def execute(self, query, params):
            if "UNWIND $rows" not in query:
                return
            assert len(params["rows"]) <= 2
            key = "statuses" if "dimension_count" in query else "values"
            self.rows[key].extend(params["rows"])

    records = [
        Record(
            "dimension_value",
            {
                "database": "db",
                "table": "dim",
                "field": f"f{field}",
                "value": f"v{value}",
            },
            "source.json",
        )
        for field in range(3)
        for value in range(3)
    ]
    monkeypatch.setattr(dimension_stage, "WRITE_BATCH_SIZE", 2, raising=False)
    storage = Storage()
    result = dimension_stage.write(records, "test", storage)
    assert result.counts == {"dimension_value": 9}
    assert [
        (s["field"], s["count"], s["samples"]) for s in storage.rows["statuses"]
    ] == [(f"f{i}", 3, ["v0", "v1", "v2"]) for i in range(3)]
    assert {(v["field"], v["value"]) for v in storage.rows["values"]} == {
        (f"f{i}", f"v{j}") for i in range(3) for j in range(3)
    }
