"""Regression tests for #351 evaluation executor project and --only handling."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_eval_exec_upgrade import (
    GOOD,
    FakeQuestionService,
    _args,
    _question,
    _write_questions,
)
from test_project_executors import (
    _create_project,
    _enable_stub_executor,
    _headers,
    _StubExecutorPlugin,
)

from semantica_dataagent import eval_exec, knowledge, plugins, pull_instances
from semantica_dataagent.db import QueryExecution, sessions
from semantica_dataagent.eval_exec import _execute, run_cli


@pytest.fixture
def fake_service():
    service = FakeQuestionService()
    try:
        yield service
    finally:
        service.close()


def test_only_accepts_spaces_around_comma_separated_ids(
    fake_service, tmp_path, monkeypatch
):
    fake_service.reset({"c03": [GOOD], "c40": [GOOD], "c41": [GOOD]})
    path = _write_questions(
        tmp_path,
        [
            _question("c03", "c03"),
            _question("c40", "c40"),
            _question("c41", "c41"),
        ],
    )
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, only="c03, c40")
    args.base_url = fake_service.base_url

    assert run_cli(args) == 0

    asked = [
        body["question"]
        for path, body in fake_service.asks
        if path.endswith("/conversations/ask")
    ]
    assert asked == ["c03", "c40"]


def test_run_cli_passes_project_to_reference_and_agent_executions(
    fake_service, tmp_path, monkeypatch
):
    fake_service.reset({"q1": [GOOD]})
    path = _write_questions(tmp_path, [_question("q1", "q1", reference_sql=GOOD)])
    monkeypatch.setenv("EVAL_EXEC_TOKEN", "secret")
    args = _args(tmp_path, path, only="q1")
    args.base_url = fake_service.base_url
    args.project = "target-project"

    executions = []
    original_execute = eval_exec._execute

    def recording_execute(client, sql, project=None):
        executions.append((sql, project))
        return original_execute(client, sql, project)

    monkeypatch.setattr(eval_exec, "_execute", recording_execute)

    assert run_cli(args) == 0
    assert executions == [(GOOD, "target-project"), (GOOD, "target-project")]


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    admin_token = initialize()
    calls: list[dict] = []
    base = dict(plugins.discover())
    base[("executor", "stub_exec")] = _StubExecutorPlugin(calls)
    monkeypatch.setattr(plugins, "discover", lambda: dict(base))
    monkeypatch.setattr(pull_instances, "discover", lambda: dict(base))

    with TestClient(app) as client:
        yield client, admin_token, calls


class _RecordingClient:
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def post(self, path, **kwargs):
        response = self.inner.post(path, **kwargs)
        self.calls.append((path, kwargs, response))
        return response


def _plan_call(client):
    return next(call for call in client.calls if call[0] == "/api/v1/queries/plan")


def _stored_project(plan_id):
    with sessions()() as db:
        row = db.get(QueryExecution, plan_id)
        assert row is not None
        return json.loads(row.plan_data)["project_id"]


def test_execute_uses_project_for_plan_and_saved_plan_for_execution(
    project_env, monkeypatch
):
    client, admin, executor_calls = project_env
    project_a = _create_project(client, admin, "Warehouse A", "warehouse")
    project_b = _create_project(client, admin, "Warehouse B", "warehouse")
    _enable_stub_executor(client, admin, project_a, "http://project-a.executor.test")
    _enable_stub_executor(client, admin, project_b, "http://project-b.executor.test")

    inspected_projects = []

    def describe(**kwargs):
        inspected_projects.append(kwargs.get("project_id"))
        return [
            {
                "database": "shop",
                "name": "orders",
                "fields": [{"name": "id", "partition": False}],
            }
        ]

    monkeypatch.setattr(knowledge, "describe", describe)
    response = client.put(
        "/api/v1/me/dtb-token",
        headers=_headers(admin),
        json={"token": "personal-token"},
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {admin}"

    recording = _RecordingClient(client)
    result = _execute(recording, "SELECT id FROM shop.orders", project=project_a)
    assert result["status"] == "succeeded"

    _, plan_kwargs, plan_response = _plan_call(recording)
    assert plan_kwargs["params"] == {"project": project_a}
    plan_id = plan_response.json()["id"]
    assert _stored_project(plan_id) == project_a
    assert project_a in inspected_projects
    assert all(
        call["endpoint"] == "http://project-a.executor.test" for call in executor_calls
    )

    executor_calls.clear()
    recording.calls.clear()

    # Negative case: a caller that passes the wrong project must not reach
    # project A's executor.  The plan, and therefore execution, follows B.
    result = _execute(recording, "SELECT id FROM shop.orders", project=project_b)
    assert result["status"] == "succeeded"
    _, plan_kwargs, plan_response = _plan_call(recording)
    assert plan_kwargs["params"] == {"project": project_b}
    assert _stored_project(plan_response.json()["id"]) == project_b
    assert project_b in inspected_projects
    assert all(
        call["endpoint"] == "http://project-b.executor.test" for call in executor_calls
    )
    assert not any(
        call["endpoint"] == "http://project-a.executor.test" for call in executor_calls
    )
