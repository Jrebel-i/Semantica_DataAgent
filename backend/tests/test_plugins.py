import json
import os
import subprocess
from pathlib import Path

from semantica_dataagent.canonical import Record
from semantica_dataagent.filters import DEFAULT_FILTERS, filter_records


def test_installed_entry_point_is_discovered(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    venv = tmp_path / "venv"
    env = os.environ.copy()
    cache = subprocess.check_output(["uv", "cache", "dir"], text=True).strip()
    env.update(
        HOME=str(tmp_path),
        UV_PROJECT_ENVIRONMENT=str(venv),
        UV_OFFLINE="1",
        UV_CACHE_DIR=cache,
    )
    env.pop("PYTHONPATH", None)
    subprocess.run(
        [
            "uv",
            "sync",
            "--directory",
            str(backend),
            "--locked",
            "--offline",
            "--no-dev",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            str(venv / "bin/python"),
            "--no-deps",
            str(backend / "tests/fixtures/third_party"),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [
            str(venv / "bin/python"),
            "-c",
            "import json; from semantica_dataagent.plugins import discover; print(json.dumps(sorted(p.id for p in discover().values())))",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert {"canonical", "third_party"}.issubset(json.loads(result.stdout))


def test_filter_defaults_and_custom_patterns():
    records = [
        Record("table", {"database": db, "name": "t"}) for db in ("tmp", "test", "prod")
    ]
    records += [
        Record("task", {"name": "daily_load"}),
        Record("task", {"name": "adhoc"}),
    ]
    kept, count = filter_records(records, DEFAULT_FILTERS)
    assert count == 2
    assert [r.data.get("database") for r in kept[:1]] == ["prod"]
    rules = {**DEFAULT_FILTERS, "database_exclude": [], "task_include": ["daily*"]}
    kept, count = filter_records(records, rules)
    assert count == 1
    assert [r.data.get("database") for r in kept[:3]] == ["tmp", "test", "prod"]
