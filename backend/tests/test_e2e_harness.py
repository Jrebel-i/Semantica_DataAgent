import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


def load_harness():
    path = Path(__file__).with_name("e2e") / "harness.py"
    name = "e2e_harness_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def write_checks(tmp_path, source):
    path = tmp_path / "ticket_checks.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_parse_defaults_and_source_specs(harness, tmp_path):
    defaults = harness.parse_args(["--scratch", str(tmp_path)])
    assert [plugin for plugin, _ in defaults.sources] == ["canonical", "markdown"]
    assert defaults.embedding_url == "http://127.0.0.1:17998"
    assert defaults.embedding_model == "model/bge-small-zh-v1.5"
    assert defaults.generation.startswith("e2e_")
    assert harness.GENERATION_RE.fullmatch(defaults.generation)

    parsed = harness.parse_args(
        [
            "--scratch",
            str(tmp_path),
            "--embedding-url",
            "http://127.0.0.1:17997",
            "--embedding-model",
            "model/bge-m3",
            "--source",
            'markdown:{"path": "docs"}',
            "--source",
            "canonical",
        ]
    )
    assert parsed.embedding_url == "http://127.0.0.1:17997"
    assert parsed.embedding_model == "model/bge-m3"
    assert parsed.sources == (("markdown", {"path": "docs"}), ("canonical", {}))


def test_parse_rejects_the_development_instances(harness, tmp_path, capsys):
    with pytest.raises(SystemExit):
        harness.parse_args(
            [
                "--scratch",
                str(tmp_path),
                "--neo4j-uri",
                "bolt://172.16.242.124:31601",
            ]
        )
    assert "development instance" in capsys.readouterr().err


def test_load_check_functions_only_finds_ticket_functions(harness, tmp_path):
    path = write_checks(
        tmp_path,
        "def check_one(ctx):\n"
        "    return True\n"
        "\n"
        "def check_two(ctx):\n"
        "    return {'ok': False, 'info': 'two'}\n"
        "\n"
        "def helper():\n"
        "    return None\n",
    )
    found = harness._load_check_functions((path,))
    assert [name for name, _ in found] == ["check_one", "check_two"]

    empty = write_checks(tmp_path, "def helper():\n    return None\n")
    with pytest.raises(RuntimeError, match="no check_\\* functions"):
        harness._load_check_functions((empty,))


def test_check_return_values_are_normalized(harness):
    assert harness._normalize_check(None) == (True, None)
    assert harness._normalize_check(True) == (True, None)
    assert harness._normalize_check(False) == (False, None)
    assert harness._normalize_check({"ok": True, "info": {"x": 1}}) == (
        True,
        {"x": 1},
    )
    assert harness._normalize_check({"info": "kept"}) == (True, {"info": "kept"})
    assert harness._normalize_check({"ok": False, "info": "bad"}) == (False, "bad")
    ok, info = harness._normalize_check("unexpected")
    assert not ok and "unsupported" in info


class FakeMcp:
    def __init__(self, events):
        self.events = events

    def call_tool(self, name, arguments):
        raise AssertionError(f"unexpected MCP call: {name}")

    def close(self):
        self.events.append("mcp_closed")


def install_fake_boundaries(harness, monkeypatch, events):
    processes = {}

    def init_database(runtime, encryption_key):
        runtime.token = "test-token"
        runtime.db_path.write_text("sqlite", encoding="utf-8")
        events.append("init")

    def start_service(runtime, encryption_key):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        runtime.server = process
        runtime.base_url = "http://127.0.0.1:1"
        runtime.service_log = runtime.config.scratch / "serve.log"
        runtime.service_handle = runtime.service_log.open("w", encoding="utf-8")
        processes["server"] = process
        events.append("start")

    def configure(runtime):
        runtime.storage_touched = True
        events.append("configure")

    def run_build(runtime):
        events.append("build")
        return {
            "id": 7,
            "status": "succeeded",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "counts": {"table": 1},
            "error": None,
            "parse_failures": [],
            "timed_out": False,
        }

    def cleanup_neo4j(runtime):
        events.append("cleanup_neo4j")
        return {"status": "absent", "deleted": 0, "remaining": 0}

    def cleanup_milvus(runtime):
        events.append("cleanup_milvus")
        return {"status": "absent", "collection": "sda_test"}

    monkeypatch.setattr(harness, "_resolve_password", lambda config: "test-password")
    monkeypatch.setattr(harness, "_init_database", init_database)
    monkeypatch.setattr(harness, "_start_service", start_service)
    monkeypatch.setattr(harness, "_wait_ready", lambda runtime: events.append("ready"))
    monkeypatch.setattr(harness, "_configure", configure)
    monkeypatch.setattr(harness, "_run_build", run_build)
    monkeypatch.setattr(harness, "_open_mcp", lambda runtime: FakeMcp(events))
    monkeypatch.setattr(harness, "_cleanup_neo4j", cleanup_neo4j)
    monkeypatch.setattr(harness, "_cleanup_milvus", cleanup_milvus)
    return processes


def result_report(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    payload = next(line for line in lines if line.startswith("E2E_RESULT "))
    done = next(line for line in lines if line.startswith("E2E_DONE "))
    return json.loads(payload.removeprefix("E2E_RESULT ")), done


def test_a_failing_ticket_check_still_runs_every_cleanup_step(
    harness, monkeypatch, tmp_path
):
    events = []
    processes = install_fake_boundaries(harness, monkeypatch, events)
    checks = write_checks(
        tmp_path, "def check_boom(ctx):\n    raise AssertionError('boom')\n"
    )
    config = harness.parse_args(
        [
            "--scratch",
            str(tmp_path / "scratch"),
            "--log",
            str(tmp_path / "e2e.log"),
            "--checks",
            str(checks),
            "--no-builtin-checks",
        ]
    )

    assert harness.run(config) == 1

    report, done = result_report(tmp_path / "e2e.log")
    assert done == "E2E_DONE exit=1"
    assert report["checks"] == [
        {
            "name": "check_boom",
            "ok": False,
            "info": "AssertionError: boom",
            "elapsed_ms": report["checks"][0]["elapsed_ms"],
        }
    ]
    assert report["checks_ok"] is False
    cleanup = report["cleanup"]
    assert cleanup.get("service", {}).get("status") == "stopped"
    assert cleanup.get("service", {}).get("process_gone") is True
    assert cleanup.get("neo4j", {}).get("status") == "absent"
    assert cleanup.get("milvus", {}).get("status") == "absent"
    assert cleanup.get("database", {}).get("status") == "deleted"
    assert events.count("cleanup_neo4j") == 1
    assert events.count("cleanup_milvus") == 1
    assert processes["server"].poll() is not None
    assert not list((tmp_path / "scratch").glob("e2e-*"))


def test_a_passing_ticket_check_exits_zero(harness, monkeypatch, tmp_path):
    install_fake_boundaries(harness, monkeypatch, [])
    checks = write_checks(
        tmp_path,
        "def check_ok(ctx):\n    return {'ok': True, 'info': {'seen': True}}\n",
    )
    config = harness.parse_args(
        [
            "--scratch",
            str(tmp_path / "scratch"),
            "--log",
            str(tmp_path / "e2e.log"),
            "--checks",
            str(checks),
            "--no-builtin-checks",
        ]
    )

    assert harness.run(config) == 0

    report, done = result_report(tmp_path / "e2e.log")
    assert done == "E2E_DONE exit=0"
    assert report["ok"] is True
    assert report["checks"][0]["info"] == {"seen": True}


def test_stop_service_terminates_a_local_process(harness):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    result = harness._stop_service(process)
    assert result["process_gone"] is True
    assert process.poll() is not None


def test_remove_run_dir_refuses_paths_outside_its_scratch(harness, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = harness.parse_args(["--scratch", str(scratch)])

    own = scratch / "e2e-owned"
    own.mkdir()
    (own / "app.db").write_text("db", encoding="utf-8")
    assert harness._remove_run_dir(config, own) == {"status": "deleted"}
    assert not own.exists()

    foreign = scratch / "other"
    foreign.mkdir()
    (foreign / "keep").write_text("keep", encoding="utf-8")
    result = harness._remove_run_dir(config, foreign)
    assert result["status"] == "refused"
    assert (foreign / "keep").is_file()

    outside = tmp_path / "e2e-outside"
    outside.mkdir()
    assert harness._remove_run_dir(config, outside)["status"] == "refused"
    assert outside.is_dir()


def test_cleanup_neo4j_deletes_large_generations_in_batches(
    harness, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    import neo4j

    class Driver:
        remaining = 5

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute_query(self, query, **params):
            assert params["generations"] == ["e2e_owned"]
            if "DETACH DELETE" in query:
                assert "LIMIT $batch_size" in query
                removed = min(self.remaining, params["batch_size"])
                self.remaining -= removed
                return ([{"n": removed}], None, None)
            return ([{"n": self.remaining}], None, None)

    driver = Driver()
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **kw: driver)
    monkeypatch.setattr(harness, "_owned_generations", lambda runtime: {"e2e_owned"})
    monkeypatch.setattr(harness, "CLEANUP_BATCH_SIZE", 2, raising=False)
    runtime = SimpleNamespace(
        config=SimpleNamespace(neo4j_uri="bolt://test", neo4j_user="neo4j"),
        password="test",
    )
    assert harness._cleanup_neo4j(runtime) == {
        "status": "deleted",
        "deleted": 5,
        "remaining": 0,
    }
