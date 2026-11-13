"""Failure diagnostics keep fixed categories and never expose raw payloads."""

import asyncio
import json
import subprocess
import threading

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

MARKER = "synthetic-secret-marker-259"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_AGENT_ROOT", str(tmp_path / "agents"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def _plugin(exc=None, calls=None):
    class Plugin:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

        async def submit(self, sql, limit):
            if calls is not None:
                calls.append(sql)
            return "internal-query-key"

        async def wait(self, key):
            if exc is not None:
                raise exc
            return {"columns": ["v"], "data": [[1]]}

        async def download(self, key, format):
            return b"v\n1\n", "text/csv"

    return Plugin()


def _configure(client, monkeypatch, plugin, tables=None):
    from semantica_dataagent import knowledge, query_api

    monkeypatch.setattr(knowledge, "describe", lambda **_: tables or [])
    monkeypatch.setattr(
        query_api,
        "_config",
        lambda: (
            plugin,
            {
                "row_limit": 100,
                "download_limit": 100,
                "confirmation_ttl_seconds": 1800,
                "timeout_seconds": 5,
            },
        ),
    )
    c, headers = client
    c.put("/api/v1/me/dtb-token", headers=headers, json={"token": "fake-token"})
    return c, headers


def _plan(c, headers, sql):
    response = c.post("/api/v1/queries/plan", headers=headers, json={"sql": sql})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _stored_query(query_id):
    from semantica_dataagent.db import QueryAudit, QueryExecution, sessions

    with sessions()() as db:
        row = db.get(QueryExecution, query_id)
        audit = (
            db.query(QueryAudit)
            .filter(QueryAudit.status.like("rejected_%"))
            .order_by(QueryAudit.created_at.desc())
            .first()
        )
        return row, audit


def test_dtb_poll_error_is_fixed_and_does_not_echo_error_message():
    from semantica_dataagent.dtb_executor import DtbError, DtbExecutor

    def handler(_request):
        return httpx.Response(
            200,
            json={
                "status": 200,
                "data": {"progress": 1, "errorMessage": f"Table missing: {MARKER}"},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            executor = DtbExecutor(
                "http://dtb.test", "token", client=client, retry_delays=()
            )
            with pytest.raises(DtbError) as error:
                await executor.poll("query-key")
            return error.value

    error = asyncio.run(run())
    assert error.failure_stage == "poll"
    assert error.failure_category == "unknown"
    assert MARKER not in str(error)
    assert MARKER not in repr(error)


def _dtb_plugin(handler):
    from semantica_dataagent.dtb_executor import DtbExecutor

    class Plugin:
        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

        def bind(self, config, token):
            return DtbExecutor(
                "http://dtb.test",
                token,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                retry_delays=(),
            )

    return Plugin()


@pytest.mark.parametrize(
    "message, category",
    [
        ("Table[orders]does not exist in database[shop]", "table_or_catalog"),
        (f"Table[ {MARKER} ]does not exist in database[shop]", "unknown"),
        (f"SQL syntax error near {MARKER}", "unknown"),
        ("prefix Table[orders]does not exist in database[shop]", "unknown"),
    ],
)
def test_dtb_poll_protocol_shape_reaches_api_and_persistence(
    client, monkeypatch, caplog, message, category
):
    tables = [
        {
            "database": "shop",
            "name": "orders",
            "fields": [{"name": "dt", "partition": True}],
        }
    ]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(
                200, json={"status": 200, "data": {"queryKey": "internal-query-key"}}
            )
        return httpx.Response(
            200,
            json={
                "status": 200,
                "data": {"progress": 1.0, "errorMessage": message},
            },
        )

    c, headers = _configure(client, monkeypatch, _dtb_plugin(handler), tables=tables)
    query_id = _plan(c, headers, "SELECT * FROM shop.orders WHERE dt = '2025-01-01'")

    with caplog.at_level("DEBUG"):
        response = c.post(
            f"/api/v1/queries/{query_id}/execute", headers=headers, json={}
        )

    assert response.status_code == 502, response.text
    assert MARKER not in response.text
    assert MARKER not in caplog.text
    row, audit = _stored_query(query_id)
    assert row.status == "failed"
    assert row.query_key == "internal-query-key"
    assert json.loads(row.plan_data)["execution_failure"] == {
        "stage": "wait",
        "category": category,
    }
    assert audit.status == "rejected_502"


def test_failure_label_rejects_unknown_diagnostic_values():
    from semantica_dataagent.failure_diagnostics import failure_details, failure_label

    exc = RuntimeError(MARKER)
    exc.failure_stage = MARKER
    exc.failure_category = "sql"

    assert failure_label(exc) == "unknown/unknown"
    assert failure_details(exc) == {"stage": "unknown", "category": "unknown"}


@pytest.mark.parametrize(
    "transport_error, category",
    [
        (httpx.ConnectError(f"connect {MARKER}"), "data_source"),
        (httpx.ReadTimeout(f"timeout {MARKER}"), "timeout"),
    ],
)
def test_dtb_retry_preserves_transport_category_without_raw_text(
    transport_error, category
):
    from semantica_dataagent.dtb_executor import DtbError, DtbExecutor

    calls = []

    def handler(request):
        calls.append(request)
        raise transport_error

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            executor = DtbExecutor(
                "http://dtb.test", "token", client=client, retry_delays=(0, 0)
            )
            with pytest.raises(DtbError) as error:
                await executor.submit("SELECT 1", 10)
            return error.value

    error = asyncio.run(run())
    assert len(calls) == 3
    assert error.failure_stage == "submit"
    assert error.failure_category == category
    assert MARKER not in str(error)
    assert MARKER not in repr(error)


@pytest.mark.parametrize(
    "failure, status, stage, category",
    [
        ("sql", 502, "wait", "sql"),
        ("auth", 401, "wait", "authentication"),
        ("timeout", 504, "wait", "timeout"),
    ],
)
def test_failed_query_keeps_stage_category_and_internal_key(
    client, monkeypatch, caplog, failure, status, stage, category
):
    from semantica_dataagent.dtb_executor import DtbAuthenticationError, DtbError

    errors = {
        "sql": DtbError("safe", stage="poll", category="sql"),
        "auth": DtbAuthenticationError("safe", stage="poll"),
        "timeout": TimeoutError(),
    }
    c, headers = _configure(client, monkeypatch, _plugin(errors[failure]))
    sql = f"SELECT '{MARKER}' AS v"
    query_id = _plan(c, headers, sql)

    with caplog.at_level("DEBUG"):
        response = c.post(
            f"/api/v1/queries/{query_id}/execute", headers=headers, json={}
        )

    assert response.status_code == status, response.text
    assert MARKER not in response.text
    assert MARKER not in caplog.text
    row, audit = _stored_query(query_id)
    assert row.status == "failed"
    assert row.rows is None
    assert row.query_key == "internal-query-key"
    assert json.loads(row.plan_data)["execution_failure"] == {
        "stage": stage,
        "category": category,
    }
    assert audit.status == f"rejected_{status}"
    assert c.get(f"/api/v1/queries/{query_id}", headers=headers).json()["rows"] is None
    assert (
        c.get(f"/api/v1/queries/{query_id}/download", headers=headers).status_code
        == 404
    )
    assert (
        c.post(
            f"/api/v1/queries/{query_id}/download-links", headers=headers
        ).status_code
        == 404
    )


def test_table_or_catalog_failure_is_distinct_from_sql_failure(
    client, monkeypatch, caplog
):
    from semantica_dataagent import knowledge

    tables = [
        {
            "database": "shop",
            "name": "orders",
            "fields": [{"name": "dt", "partition": True}],
        }
    ]
    c, headers = _configure(client, monkeypatch, _plugin(), tables=tables)
    sql = f"SELECT * FROM shop.orders WHERE dt = '{MARKER}'"
    query_id = _plan(c, headers, sql)
    monkeypatch.setattr(knowledge, "describe", lambda **_: [])

    with caplog.at_level("DEBUG"):
        response = c.post(
            f"/api/v1/queries/{query_id}/execute", headers=headers, json={}
        )

    assert response.status_code == 422, response.text
    assert MARKER not in response.text
    assert MARKER not in caplog.text
    row, _ = _stored_query(query_id)
    assert row.query_key is None
    assert json.loads(row.plan_data)["execution_failure"] == {
        "stage": "table_catalog",
        "category": "table_or_catalog",
    }


def test_execution_revalidation_records_sql_category(client, monkeypatch):
    from semantica_dataagent.db import QueryExecution, sessions

    c, headers = _configure(client, monkeypatch, _plugin())
    query_id = _plan(c, headers, f"SELECT '{MARKER}' AS v")
    with sessions().begin() as db:
        db.get(QueryExecution, query_id).sql = f"SELECT FROM {MARKER}"

    response = c.post(f"/api/v1/queries/{query_id}/execute", headers=headers, json={})

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": "SQL 校验失败"}
    assert MARKER not in response.text
    row, _ = _stored_query(query_id)
    assert json.loads(row.plan_data)["execution_failure"] == {
        "stage": "validate_sql",
        "category": "sql",
    }


def test_metadata_read_failure_keeps_data_source_category(client, monkeypatch):
    from semantica_dataagent import knowledge

    c, headers = _configure(client, monkeypatch, _plugin())
    query_id = _plan(c, headers, "SELECT 1 AS v")

    def unavailable(**_kwargs):
        raise RuntimeError(f"graph down {MARKER}")

    monkeypatch.setattr(knowledge, "describe", unavailable)
    response = c.post(f"/api/v1/queries/{query_id}/execute", headers=headers, json={})

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "查询未通过目录或分区校验"}
    assert MARKER not in response.text
    row, _ = _stored_query(query_id)
    assert row.query_key is None
    assert json.loads(row.plan_data)["execution_failure"] == {
        "stage": "table_catalog",
        "category": "data_source",
    }


@pytest.mark.parametrize(
    "sql",
    [
        f'SELECT * FROM TABLE(GENERATE_SERIES(1, 10)) AS "alias_{MARKER}"',
        f"SELECT * FROM missing_table AS \"alias_{MARKER}\" WHERE dt = '{MARKER}'",
    ],
)
def test_partition_revalidation_never_echoes_aliases_or_literals(
    client, monkeypatch, sql
):
    from semantica_dataagent.db import QueryExecution, sessions

    c, headers = _configure(client, monkeypatch, _plugin())
    query_id = _plan(c, headers, "SELECT 1 AS v")
    with sessions().begin() as db:
        db.get(QueryExecution, query_id).sql = sql

    response = c.post(f"/api/v1/queries/{query_id}/execute", headers=headers, json={})

    assert response.status_code == 422, response.text
    assert MARKER not in response.text
    row, _ = _stored_query(query_id)
    assert json.loads(row.plan_data)["execution_failure"] == {
        "stage": "table_catalog",
        "category": "table_or_catalog",
    }


def _session_runtime(tmp_path):
    from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox

    runtime = object.__new__(KubernetesSessionSandbox)
    runtime.workspace = tmp_path
    runtime.name = "sda-agent-test"
    runtime.spec = {}
    runtime.lock = threading.RLock()
    runtime.closed = False
    runtime.ready = False
    runtime._upload = lambda: None
    return runtime


def test_k8s_start_keeps_wait_stage_and_safe_pod_status(tmp_path, monkeypatch):
    from semantica_dataagent.k8s_sandbox import SessionStartError

    runtime = _session_runtime(tmp_path)
    calls = []

    def run(*args, input=None, check=True, timeout=150):
        calls.append((args, check))
        if args[0] == "wait":
            raise subprocess.CalledProcessError(
                1,
                ["kubectl", "wait", MARKER],
                output=MARKER,
                stderr=f"stderr {MARKER}",
            )
        if args[0] == "get":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {
                        "status": {
                            "phase": "Pending",
                            "reason": "Unschedulable",
                            "message": f"pod message {MARKER}",
                            "containerStatuses": [
                                {
                                    "state": {
                                        "waiting": {
                                            "reason": "CreateContainerError",
                                            "message": f"stdin {MARKER}",
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    runtime._run = run

    with pytest.raises(SessionStartError) as error:
        runtime.start()

    failure = error.value
    assert failure.failure_stage == "wait"
    assert failure.failure_category == "nonzero_exit"
    assert failure.pod_status == {
        "phase": "Pending",
        "reason": "Unschedulable",
        "container_reason": "CreateContainerError",
    }
    assert MARKER not in str(failure)
    assert MARKER not in repr(failure)
    assert [args[0] for args, _ in calls] == ["create", "wait", "get", "delete"]


def test_k8s_upload_failure_is_named_and_cleanup_failure_does_not_override(
    tmp_path,
):
    from semantica_dataagent.k8s_sandbox import SessionStartError

    runtime = _session_runtime(tmp_path)

    def upload():
        raise subprocess.CalledProcessError(
            1,
            ["kubectl", "exec", "-i", MARKER],
            output=MARKER,
            stderr=MARKER,
        )

    def run(*args, input=None, check=True, timeout=150):
        if args[0] == "delete":
            raise subprocess.CalledProcessError(
                1, ["kubectl", "delete", MARKER], stderr=MARKER
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    runtime._upload = upload
    runtime._run = run

    with pytest.raises(SessionStartError) as error:
        runtime.start()

    assert error.value.failure_stage == "upload"
    assert error.value.failure_category == "nonzero_exit"
    assert MARKER not in str(error.value)
    assert MARKER not in repr(error.value)


def test_k8s_keyboard_interrupt_keeps_lifecycle_semantics(tmp_path):
    runtime = _session_runtime(tmp_path)
    calls = []

    def run(*args, input=None, check=True, timeout=150):
        calls.append(args[0])
        if args[0] == "create":
            raise KeyboardInterrupt()
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    runtime._run = run

    with pytest.raises(KeyboardInterrupt):
        runtime.start()
    assert calls == ["create", "delete"]


def test_k8s_pod_status_values_are_allowlisted_and_use_short_timeout(tmp_path):
    from semantica_dataagent.k8s_sandbox import (
        POD_STATUS_TIMEOUT_SECONDS,
        SessionStartError,
    )

    runtime = _session_runtime(tmp_path)
    calls = []

    def run(*args, input=None, check=True, timeout=150):
        calls.append((args[0], timeout))
        if args[0] == "get":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {
                        "status": {
                            "phase": MARKER,
                            "reason": MARKER,
                            "message": MARKER,
                            "containerStatuses": [
                                {
                                    "state": {
                                        "waiting": {
                                            "reason": MARKER,
                                            "message": MARKER,
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ),
                stderr=MARKER,
            )
        if args[0] == "wait":
            raise subprocess.CalledProcessError(
                1, ["kubectl", "wait", MARKER], output=MARKER, stderr=MARKER
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    runtime._run = run
    status = runtime._pod_status()
    assert status == {
        "phase": "unknown",
        "reason": "unknown",
        "container_reason": "unknown",
    }
    assert MARKER not in repr(status)

    with pytest.raises(SessionStartError) as error:
        runtime.start()

    failure = error.value
    assert failure.failure_stage == "wait"
    assert failure.failure_category == "nonzero_exit"
    assert failure.pod_status == {
        "phase": "unknown",
        "reason": "unknown",
        "container_reason": "unknown",
    }
    assert MARKER not in str(failure)
    assert MARKER not in repr(failure)
    diagnostic_calls = [timeout for name, timeout in calls if name == "get"]
    assert diagnostic_calls == [POD_STATUS_TIMEOUT_SECONDS] * 2
    assert POD_STATUS_TIMEOUT_SECONDS < 150
    assert [name for name, _ in calls] == ["get", "create", "wait", "get", "delete"]


def test_k8s_pod_status_timeout_does_not_mask_first_error(tmp_path):
    from semantica_dataagent.k8s_sandbox import SessionStartError

    runtime = _session_runtime(tmp_path)
    calls = []

    def run(*args, input=None, check=True, timeout=150):
        calls.append(args[0])
        if args[0] == "wait":
            raise subprocess.TimeoutExpired(["kubectl", "wait", MARKER], 120)
        if args[0] == "get":
            raise subprocess.TimeoutExpired(["kubectl", "get", MARKER], timeout)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    runtime._run = run

    with pytest.raises(SessionStartError) as error:
        runtime.start()

    failure = error.value
    assert failure.failure_stage == "wait"
    assert failure.failure_category == "timeout"
    assert failure.pod_status is None
    assert MARKER not in str(failure)
    assert MARKER not in repr(failure)
    assert "delete" in calls


def test_k8s_late_cancellation_during_diagnostics_runs_cleanup(tmp_path):
    runtime = _session_runtime(tmp_path)
    calls = []

    def run(*args, input=None, check=True, timeout=150):
        calls.append(args[0])
        if args[0] == "wait":
            raise subprocess.CalledProcessError(
                1, ["kubectl", "wait", MARKER], output=MARKER, stderr=MARKER
            )
        if args[0] == "get":
            raise KeyboardInterrupt()
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    runtime._run = run

    with pytest.raises(KeyboardInterrupt):
        runtime.start()
    assert calls == ["create", "wait", "get", "delete"]


def test_k8s_cleanup_cancellation_is_not_swallowed(tmp_path):
    runtime = _session_runtime(tmp_path)

    def run(*args, input=None, check=True, timeout=150):
        if args[0] == "wait":
            raise subprocess.CalledProcessError(
                1, ["kubectl", "wait", MARKER], output=MARKER, stderr=MARKER
            )
        if args[0] == "delete":
            raise KeyboardInterrupt()
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    runtime._run = run

    with pytest.raises(KeyboardInterrupt):
        runtime.start()


class ExplodingRunner:
    agent_id = "pi"

    def available(self):
        return True

    async def run(self, request):
        from semantica_dataagent.k8s_sandbox import SessionStartError

        raise SessionStartError(
            "wait",
            "nonzero_exit",
            pod_status={"phase": "Pending", "message": f"raw {MARKER}"},
        )
        yield  # pragma: no cover - make this an async generator


def _sse(body):
    events = []
    for block in body.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None:
            events.append((name, data))
    return events


def test_session_failure_reaches_sse_and_persisted_turn_without_raw_text(
    client, monkeypatch, caplog
):
    from semantica_dataagent import agent_runner

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", ExplodingRunner())
    c, headers = client
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["test-model"],
        },
    ).json()

    with (
        caplog.at_level("DEBUG"),
        c.stream(
            "POST",
            "/api/v1/conversations/ask",
            headers=headers,
            json={
                "question": "test",
                "agent": "pi",
                "provider_id": provider["id"],
                "model": "test-model",
            },
        ) as response,
    ):
        body = "".join(response.iter_text())

    assert MARKER not in body
    assert MARKER not in caplog.text
    events = _sse(body)
    conversation_id = events[0][1]["conversation_id"]
    error = next(data for name, data in events if name == "error")
    assert error["message"] == "Agent failed: wait/nonzero_exit"
    assert error["failure"] == {
        "stage": "wait",
        "category": "nonzero_exit",
        "pod_status": {"phase": "Pending"},
    }
    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
    turn = detail["turns"][0]
    assert turn["status"] == "failed"
    assert turn["error"] == "Agent failed: wait/nonzero_exit"
    persisted_error = next(
        event for event in turn["events"] if event["type"] == "error"
    )
    assert persisted_error["data"]["failure"] == {
        "stage": "wait",
        "category": "nonzero_exit",
        "pod_status": {"phase": "Pending"},
    }
    assert MARKER not in json.dumps(turn, ensure_ascii=False)


class UnknownFailureRunner:
    agent_id = "pi"

    def available(self):
        return True

    async def run(self, request):
        raise RuntimeError(f"unexpected {MARKER}")
        yield  # pragma: no cover - make this an async generator


def test_unknown_agent_failure_degrades_to_fixed_unknown_in_sse_and_storage(
    client, monkeypatch, caplog
):
    from semantica_dataagent import agent_runner

    monkeypatch.setitem(agent_runner.RUNNERS, "pi", UnknownFailureRunner())
    c, headers = client
    provider = c.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "name": "Local",
            "base_url": "http://127.0.0.1:3425",
            "models": ["test-model"],
        },
    ).json()

    with (
        caplog.at_level("DEBUG"),
        c.stream(
            "POST",
            "/api/v1/conversations/ask",
            headers=headers,
            json={
                "question": "test",
                "agent": "pi",
                "provider_id": provider["id"],
                "model": "test-model",
            },
        ) as response,
    ):
        body = "".join(response.iter_text())

    assert MARKER not in body
    assert MARKER not in caplog.text
    events = _sse(body)
    conversation_id = events[0][1]["conversation_id"]
    error = next(data for name, data in events if name == "error")
    assert error["message"] == "Agent failed: unknown/unknown"
    assert error["failure"] == {"stage": "unknown", "category": "unknown"}
    detail = c.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()
    turn = detail["turns"][0]
    assert turn["status"] == "failed"
    assert turn["error"] == "Agent failed: unknown/unknown"
    persisted_error = next(
        event for event in turn["events"] if event["type"] == "error"
    )
    assert persisted_error["data"]["failure"] == {
        "stage": "unknown",
        "category": "unknown",
    }
    assert MARKER not in json.dumps(turn, ensure_ascii=False)
