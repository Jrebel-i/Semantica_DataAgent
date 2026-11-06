from semantica_dataagent.canonical import Record
from semantica_dataagent.dtb_tasks import DtbTaskParser
from semantica_dataagent.sql_tasks import SQLTaskParser

LIVY = r"""#!/bin/sh
origin_schema="ods"
origin_table="air_payments_raw"
target_schema="dwd"
target_table="air_payments_d"
create_temp_view="CREATE TEMPORARY VIEW mysql_air_payments USING org.apache.spark.sql.jdbc OPTIONS (url 'jdbc:mysql://${mysql_host}:3306/ods',dbtable '\`ods\`.\`air_payments_raw\`',user 'reader')"
insert_data="INSERT OVERWRITE TABLE ${target_schema}.${target_table} PARTITION(day_num='${day_num}') SELECT payment_id FROM mysql_air_payments;"
submit_sql_and_wait "${create_temp_view}"
submit_sql_and_wait "${insert_data}"
"""

BEELINE = """#!/bin/sh
airport="PVG"
beeline -u jdbc:hive2://${hive_host}:10000 -n hadoop --silent=true -e "select count(*) from dimensions.dim_airport where airport_code = '${airport}'"
"""

BASIC = """#!/bin/sh
origin_schema="ods"
origin_table="air_tickets_raw"
target_schema="dwd"
target_table="air_tickets_d"
hdfs dfs -ls /warehouse/${target_schema}.db/${target_table}
"""


def task(task_type, sql, parameters=None):
    data = {"name": "fixture", "type": task_type, "sql": sql}
    if parameters is not None:
        data["parameters"] = parameters
    return Record("task", data)


def test_livy_submissions_are_expanded_and_jdbc_sources_resolved():
    result = DtbTaskParser().parse(task("shell", LIVY), {})
    assert result.status == "success"
    assert result.reason == "livy"
    assert result.reads == ["ods.air_payments_raw"]
    assert result.writes == ["dwd.air_payments_d"]
    assert result.edges == [("ods.air_payments_raw", "dwd.air_payments_d")]


def test_beeline_submissions_are_parsed():
    result = DtbTaskParser().parse(task("shell", BEELINE), {})
    assert result.status == "success"
    assert result.reason == "beeline"
    assert result.reads == ["dimensions.dim_airport"]
    assert result.writes == []
    assert result.edges == []


def test_shell_basic_regex_fallback_builds_cross_product():
    source = BASIC + 'hive -e "select 1 from raw.extra"\n'
    result = DtbTaskParser().parse(task("shell", source), {})
    assert result.status == "success"
    assert result.reason == "shell_basic"
    assert result.reads == ["ods.air_tickets_raw", "raw.extra"]
    assert result.writes == ["dwd.air_tickets_d"]
    assert set(result.edges) == {
        ("ods.air_tickets_raw", "dwd.air_tickets_d"),
        ("raw.extra", "dwd.air_tickets_d"),
    }


def test_unparseable_sql_falls_back_to_shell_basic():
    source = BASIC + 'submit_sql_and_wait "INSERT INTO x SELECT FROM"\n'
    result = DtbTaskParser().parse(task("shell", source), {})
    assert result.status == "success"
    assert result.reason == "shell_basic (sql parse failed)"
    assert result.edges == [("ods.air_tickets_raw", "dwd.air_tickets_d")]


def test_shell_without_any_recognized_tables_fails():
    result = DtbTaskParser().parse(task("shell", "#!/bin/sh\necho hello\n"), {})
    assert result.status == "failed"
    assert "No tables recognized" in result.reason


def test_merge_onself_reads_nested_parameters_and_suppresses_self_edge():
    result = DtbTaskParser().parse(
        task("merge_onself", "", {"params": {"table": "dwd.air_orders_d"}}), {}
    )
    assert result.status == "success"
    assert result.reason == "merge_onself"
    assert result.reads == ["dwd.air_orders_d"]
    assert result.writes == ["dwd.air_orders_d"]
    assert result.edges == []


def test_merge_onself_without_target_is_unknown():
    for parameters in ({}, {"params": {}}, None):
        result = DtbTaskParser().parse(task("merge_onself", "", parameters), {})
        assert result.status == "failed"
        assert "unknown" in result.reason


def test_registry_dispatches_spark_to_generic_sql_parser(monkeypatch):
    from semantica_dataagent import plugins

    monkeypatch.setattr(
        plugins,
        "configured",
        lambda: [
            ("parser", SQLTaskParser(), True, {}),
            ("parser", DtbTaskParser(), True, {}),
        ],
    )
    spark = task(
        "spark",
        "INSERT OVERWRITE TABLE dwd.air_orders_d SELECT * FROM ods.air_orders_raw",
    )
    parsed = plugins.parse_task(spark)
    assert parsed.status == "success"
    assert parsed.edges == [("ods.air_orders_raw", "dwd.air_orders_d")]

    unknown = plugins.parse_task(task("python", "print(1)"))
    assert unknown.status == "unsupported"


def test_installed_entry_points_register_dtb_plugins():
    from semantica_dataagent.plugins import discover

    found = discover()
    assert ("source", "dtb") in found
    assert ("parser", "dtb") in found


def test_shell_with_large_catalog_finishes_promptly():
    import time

    schema = {f"db.table_{i}": [f"column_{j}" for j in range(25)] for i in range(1800)}
    schema.update({"src.items": ["id"], "dst.items": ["id"]})
    source = ('# filler line with no shell command\n' * 100) + (
        'query="INSERT INTO dst.items SELECT id FROM src.items"\n'
        'submit_sql_and_wait "$query"\n'
    )
    started = time.monotonic()
    result = DtbTaskParser().parse(task("shell", source), {"schema": schema})
    elapsed = time.monotonic() - started
    assert result.column_edges == [("src.items", "id", "dst.items", "id")]
    assert elapsed < 0.5, f"shell parsing took {elapsed:.3f}s"
