"""REST contract for query execution and caller-owned DTB credentials."""

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {token}"}


def test_personal_dtb_token_is_encrypted_and_never_echoed(client):
    c, headers = client
    secret = "distinct-personal-dtb-token"
    response = c.put("/api/v1/me/dtb-token", headers=headers, json={"token": secret})
    assert response.status_code == 200, response.text
    assert secret not in response.text
    response = c.get("/api/v1/me/dtb-token", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"configured": True}
    assert secret not in Path(os.environ["DATAAGENT_DB_PATH"]).read_bytes().decode(
        "latin1"
    )


def test_query_plan_rejects_non_select(client):
    c, headers = client
    response = c.post(
        "/api/v1/queries/plan", headers=headers, json={"sql": "DELETE FROM shop.orders"}
    )
    assert response.status_code == 422, response.text
    assert "SELECT" in response.text


def test_query_endpoints_require_authentication(client):
    c, _ = client
    assert c.post("/api/v1/queries/plan", json={"sql": "SELECT 1"}).status_code == 401
    assert c.get("/api/v1/me/dtb-token").status_code == 401


def _configure_partition_plan(monkeypatch):
    from semantica_dataagent import knowledge, query_api

    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda **_: [
            {
                "database": "shop",
                "name": "orders",
                "fields": [{"name": "dt", "partition": True}],
            },
            {
                "database": "shop",
                "name": "customers",
                "fields": [{"name": "dt", "partition": True}],
            },
        ],
    )

    class Plugin:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

    monkeypatch.setattr(query_api, "_config", lambda: (Plugin(), {}))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)",
        (
            "SELECT o.* FROM shop.orders AS o "
            "WHERE o.dt = (SELECT MAX(dt) FROM shop.orders)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders) AND dt >= '2025-01-01'"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders) "
            "AND amount > 0"
        ),
        (
            "SELECT o.* FROM shop.orders AS o "
            "WHERE o.dt = (SELECT MAX(o.dt) FROM shop.orders AS o)"
        ),
    ],
)
def test_latest_partition_subquery_is_accepted(client, monkeypatch, sql):
    c, headers = client
    _configure_partition_plan(monkeypatch)

    response = c.post("/api/v1/queries/plan", headers=headers, json={"sql": sql})

    assert response.status_code == 200, response.text
    plan = response.json()
    assert set(plan["tables"]) == {"shop.orders"}
    assert plan["estimated_partitions"] is None
    assert plan["estimated_rows"] is None
    assert plan["partition_selection"] == "latest_at_runtime"


@pytest.mark.parametrize(
    "sql",
    [
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders WHERE amount > 0)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders GROUP BY dt)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders HAVING MAX(dt) > '2025-01-01')"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders LIMIT 1)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders JOIN shop.customers ON 1 = 1)"
        ),
        (
            "WITH latest AS (SELECT MAX(dt) AS dt FROM shop.orders) "
            "SELECT * FROM shop.orders WHERE dt = (SELECT dt FROM latest)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = (WITH latest AS "
            "(SELECT MAX(dt) AS dt FROM shop.orders) "
            "SELECT MAX(dt) FROM shop.orders)"
        ),
        "SELECT * FROM shop.orders WHERE dt = (SELECT MAX(amount) FROM shop.orders)",
        "SELECT * FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.customers)",
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders) OR 1 = 1"
        ),
        "SELECT * FROM shop.orders WHERE NOT (dt = (SELECT MAX(dt) FROM shop.orders))",
        (
            "SELECT (SELECT MAX(dt) FROM shop.orders), * FROM shop.orders "
            "WHERE dt = '2025-01-01'"
        ),
        "SELECT * FROM shop.orders WHERE dt >= (SELECT MAX(dt) FROM shop.orders)",
        "SELECT * FROM shop.orders WHERE dt IN (SELECT MAX(dt) FROM shop.orders)",
        "SELECT MAX(dt) FROM shop.orders",
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM (SELECT dt FROM shop.orders) AS latest)"
        ),
        "SELECT * FROM shop.orders WHERE dt = (SELECT MAX(dt) + 1 FROM shop.orders)",
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT DISTINCT MAX(dt) FROM shop.orders)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(DISTINCT dt) FROM shop.orders)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) OVER () FROM shop.orders)"
        ),
        (
            "SELECT * FROM shop.orders WHERE dt = "
            "(SELECT MAX(dt) FROM shop.orders ORDER BY MAX(dt))"
        ),
    ],
)
def test_invalid_latest_partition_subquery_is_rejected(client, monkeypatch, sql):
    c, headers = client
    _configure_partition_plan(monkeypatch)

    response = c.post("/api/v1/queries/plan", headers=headers, json={"sql": sql})

    assert response.status_code == 422, response.text
    assert "缺少分区条件" in response.text


def test_latest_partition_execution_is_still_limited(client, monkeypatch):
    from semantica_dataagent import knowledge, query_api

    submissions = []

    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda **_: [
            {
                "database": "shop",
                "name": "orders",
                "fields": [{"name": "dt", "partition": True}],
            }
        ],
    )

    class Plugin:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

        async def submit(self, sql, limit):
            submissions.append((sql, limit))
            return "query-key"

        async def wait(self, key):
            return {"columns": ["dt"], "data": [["2025-01-01"]]}

    monkeypatch.setattr(
        query_api,
        "_config",
        lambda: (Plugin(), {"row_limit": 10, "download_limit": 5}),
    )
    c, headers = client
    assert (
        c.put(
            "/api/v1/me/dtb-token", headers=headers, json={"token": "fake-token"}
        ).status_code
        == 200
    )
    response = c.post(
        "/api/v1/queries/plan",
        headers=headers,
        json={
            "sql": "SELECT * FROM shop.orders "
            "WHERE dt = (SELECT MAX(dt) FROM shop.orders)"
        },
    )
    assert response.status_code == 200, response.text
    query_id = response.json()["id"]

    response = c.post(
        f"/api/v1/queries/{query_id}/execute", headers=headers, json={"limit": 1}
    )

    assert response.status_code == 200, response.text
    assert len(submissions) == 1
    bounded, limit = submissions[0]
    assert bounded.startswith("SELECT * FROM (SELECT * FROM shop.orders")
    assert bounded.endswith(") AS _dataagent_query LIMIT 5")
    assert "MAX(dt)" in bounded
    assert limit == 5


def test_expired_query_confirmation_cannot_execute(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from semantica_dataagent import knowledge, query_api
    from semantica_dataagent.db import QueryExecution, sessions

    class Executor:
        async def estimate(self, sql):
            return {"partitions": None, "rows": None}

        async def submit(self, sql, limit):
            return "fake-query"

        async def wait(self, key):
            return {"columns": ["value"], "data": [[1]]}

    class Plugin:
        def bind(self, config, token):
            return Executor()

    monkeypatch.setattr(knowledge, "describe", lambda **_: [])
    monkeypatch.setattr(query_api, "_config", lambda: (Plugin(), {}))
    c, headers = client
    c.put("/api/v1/me/dtb-token", headers=headers, json={"token": "test-token"})
    plan = c.post("/api/v1/queries/plan", headers=headers, json={"sql": "SELECT 1"})
    assert plan.status_code == 200, plan.text
    query_id = plan.json()["id"]
    with sessions().begin() as db:
        db.get(QueryExecution, query_id).created_at = datetime.now(UTC) - timedelta(
            minutes=31
        )
    response = c.post(f"/api/v1/queries/{query_id}/execute", headers=headers, json={})
    assert response.status_code == 410, response.text


def test_mcp_proposes_only_validated_sql_without_executing(client, monkeypatch):
    import asyncio

    from mcp.types import CallToolRequest, CallToolRequestParams

    from semantica_dataagent import knowledge, query_api, sql_validation
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Token, sessions
    from semantica_dataagent.mcp_context import bind_mcp_context

    submissions = []

    class Plugin:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": 3, "rows": 20}

        async def submit(self, sql, limit):
            submissions.append(sql)

    monkeypatch.setattr(knowledge, "describe", lambda **_: [])
    monkeypatch.setattr(query_api, "_config", lambda: (Plugin(), {"row_limit": 100}))
    monkeypatch.setattr(
        sql_validation, "validate_sql", lambda sql, dialect: {"status": "pass"}
    )
    _, headers = client
    token = headers["Authorization"].removeprefix("Bearer ")

    async def run():
        server = create_mcp()
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]
        with sessions()() as db:
            owner = db.query(Token).first()
            requester = Requester(id=owner.id, role=owner.role, token=token)
        with bind_mcp_context(requester):
            return (
                await handler(
                    CallToolRequest(
                        method="tools/call",
                        params=CallToolRequestParams(
                            name="propose_query", arguments={"sql": "SELECT 1"}
                        ),
                    )
                )
            ).root

    result = asyncio.run(run())
    assert not result.isError
    plan = result.structuredContent
    assert plan["sql"] == "SELECT 1"
    assert plan["data_source"] == "doris_ai"
    assert plan["estimated_partitions"] == 3
    assert plan["estimated_rows"] == 20
    assert plan["row_limit"] == 100
    assert plan["id"]
    assert not submissions

    monkeypatch.setattr(
        sql_validation, "validate_sql", lambda sql, dialect: {"status": "issues"}
    )
    rejected = asyncio.run(run())
    assert rejected.isError
    assert not submissions


def test_personal_credentials_are_isolated_between_callers(client):
    c, admin = client
    user = c.post(
        "/api/v1/tokens", headers=admin, json={"name": "other", "role": "user"}
    ).json()["token"]
    other = {"Authorization": f"Bearer {user}"}
    assert (
        c.put(
            "/api/v1/me/dtb-token", headers=admin, json={"token": "admin-secret"}
        ).status_code
        == 200
    )
    assert c.get("/api/v1/me/dtb-token", headers=other).json() == {"configured": False}
    assert (
        c.put(
            "/api/v1/me/dtb-token", headers=other, json={"token": "other-secret"}
        ).status_code
        == 200
    )
    assert c.put("/api/v1/me/dtb-token", headers=admin, json={"token": ""}).json() == {
        "configured": False
    }
    assert c.get("/api/v1/me/dtb-token", headers=other).json() == {"configured": True}


def test_partition_safety_and_execution(client, monkeypatch):
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from semantica_dataagent import knowledge

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.headers.get("token"), body))
            payload = json.dumps(
                {"status": 200, "data": {"queryKey": "query1"}}
            ).encode()
            if self.headers.get("token") == "expired":
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path.startswith("/api/data-query/result/"):
                payload = json.dumps(
                    {
                        "status": 200,
                        "data": {
                            "progress": 1,
                            "columns": [{"name": "v", "type": "long"}],
                            "data": [[1], [2]],
                        },
                    }
                ).encode()
            else:
                payload = b"v\n1\n2\n"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(
            knowledge,
            "describe",
            lambda **_: [
                {
                    "database": "shop",
                    "name": "orders",
                    "fields": [{"name": "dt", "partition": True}],
                }
            ],
        )
        c, headers = client
        base_url = f"http://127.0.0.1:{server.server_port}"
        response = c.put(
            "/api/v1/plugins/executor/dtb",
            headers=headers,
            json={
                "enabled": True,
                "parameters": {
                    "base_url": base_url,
                    "row_limit": 1,
                    "download_limit": 5,
                },
            },
        )
        assert response.status_code == 200, response.text
        for sql in [
            "select * from shop.orders",
            "select * from shop.orders where dt = '2025' for update",
            "select * from shop.orders where dt = '2025' or 1=1",
        ]:
            response = c.post(
                "/api/v1/queries/plan", headers=headers, json={"sql": sql}
            )
            assert response.status_code == 422, response.text
        monkeypatch.setattr(
            knowledge,
            "describe",
            lambda **_: [
                {"database": "shop", "name": "orders", "fields": [{"name": "dt"}]}
            ],
        )
        assert (
            c.post(
                "/api/v1/queries/plan",
                headers=headers,
                json={"sql": "select * from shop.orders"},
            ).status_code
            == 503
        )
        monkeypatch.setattr(
            knowledge,
            "describe",
            lambda **_: [
                {
                    "database": "shop",
                    "name": "orders",
                    "fields": [{"name": "dt", "partition": True}],
                }
            ],
        )
        response = c.post(
            "/api/v1/queries/plan",
            headers=headers,
            json={"sql": "select * from shop.orders where dt = '2025'"},
        )
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["estimated_rows"] is None
        assert plan["execution_limit"] == 5
        assert (
            c.put(
                "/api/v1/plugins/executor/dtb",
                headers=headers,
                json={
                    "enabled": True,
                    "parameters": {
                        "base_url": base_url,
                        "row_limit": 1,
                        "download_limit": 50,
                    },
                },
            ).status_code
            == 200
        )
        path = "/api/v1/queries/" + plan["id"]
        assert (
            c.post(path + "/execute", headers=headers, json={"limit": 1}).status_code
            == 400
        )
        c.put("/api/v1/me/dtb-token", headers=headers, json={"token": "caller-token"})
        assert (
            c.post(path + "/execute", headers=headers, json={"limit": 2}).status_code
            == 422
        )
        response = c.post(path + "/execute", headers=headers, json={})
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [[1]]
        assert response.json()["columns"] == ["v"]
        assert calls[0][0] == "caller-token"
        assert calls[0][1]["dataSource"] == "doris_ai"
        assert calls[0][1]["defaultLimit"] == 5
        assert c.get(path, headers=headers).json()["status"] == "succeeded"
        assert c.get(path + "/download", headers=headers).content == b"v\n1\n2\n"
        audit = c.get("/api/v1/query-audit", headers=headers).json()
        assert sorted(item["status"] for item in audit) == [
            "rejected_400",
            "rejected_422",
            "succeeded",
        ]
        assert (
            next(item for item in audit if item["status"] == "succeeded")["row_count"]
            == 2
        )
        c.put("/api/v1/me/dtb-token", headers=headers, json={"token": "expired"})
        plan2 = c.post(
            "/api/v1/queries/plan",
            headers=headers,
            json={"sql": "select * from shop.orders where dt = '2025'"},
        ).json()
        response = c.post(
            "/api/v1/queries/" + plan2["id"] + "/execute",
            headers=headers,
            json={"limit": 1},
        )
        assert response.status_code == 401 and "token" in response.text
        assert any(
            item["status"] == "rejected_401"
            for item in c.get("/api/v1/query-audit", headers=headers).json()
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_interrupted_execution_preserves_audit(tmp_path, monkeypatch):
    import multiprocessing
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "crash.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        knowledge,
        "describe",
        lambda **_: [
            {
                "database": "shop",
                "name": "orders",
                "fields": [{"name": "dt", "partition": True}],
            }
        ],
    )
    entered, release = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            entered.set()
            release.wait(15)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as c:
            assert (
                c.put(
                    "/api/v1/plugins/executor/dtb",
                    headers=headers,
                    json={
                        "enabled": True,
                        "parameters": {
                            "base_url": f"http://127.0.0.1:{server.server_port}"
                        },
                    },
                ).status_code
                == 200
            )
            assert (
                c.put(
                    "/api/v1/me/dtb-token",
                    headers=headers,
                    json={"token": "crash-test"},
                ).status_code
                == 200
            )
            plan = c.post(
                "/api/v1/queries/plan",
                headers=headers,
                json={"sql": "select * from shop.orders where dt = '2025'"},
            ).json()
        path = "/api/v1/queries/" + plan["id"]

        def run():
            with TestClient(app) as child:
                child.post(path + "/execute", headers=headers, json={"limit": 1})

        process = multiprocessing.get_context("fork").Process(target=run)
        process.start()
        try:
            assert entered.wait(10), "execution did not reach DTB"
            process.kill()
            process.join(10)
            assert not process.is_alive()
        finally:
            if process.is_alive():
                process.kill()
                process.join(10)
        with TestClient(app) as c:
            assert c.get(path, headers=headers).json()["status"] == "failed"
            assert (
                c.get("/api/v1/query-audit", headers=headers).json()[0]["status"]
                == "interrupted"
            )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_local_mcp_confirmation_and_download(client, monkeypatch):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from mcp.types import CallToolRequest, CallToolRequestParams

    from semantica_dataagent import knowledge, query_api
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import QueryAudit, QueryExecution, Token, sessions
    from semantica_dataagent.mcp_context import bind_mcp_context

    calls = []

    class Plugin:
        def bind(self, config, token):
            return self

        async def estimate(self, sql):
            return {"partitions": 1, "rows": 2}

        async def submit(self, sql, limit):
            calls.append(sql)
            return "key"

        async def wait(self, key):
            return {"columns": ["value"], "data": [[1], [2]]}

    monkeypatch.setattr(knowledge, "describe", lambda **_: [])
    monkeypatch.setattr(query_api, "_config", lambda: (Plugin(), {"row_limit": 2}))
    c, headers = client
    c.put("/api/v1/me/dtb-token", headers=headers, json={"token": "fake"})
    other_token = c.post(
        "/api/v1/tokens", headers=headers, json={"name": "other", "role": "user"}
    ).json()["token"]
    with sessions()() as db:
        owners = db.query(Token).all()
        owner = next(x for x in owners if x.role == "admin")
        other = next(x for x in owners if x.role == "user")
        first = Requester(
            id=owner.id, role=owner.role, token=headers["Authorization"][7:]
        )
        second = Requester(id=other.id, role=other.role, token=other_token)

    async def run():
        server = create_mcp()
        server.get_context = lambda: None
        handler = server._mcp_server.request_handlers[CallToolRequest]

        async def call(user, args):
            with bind_mcp_context(user):
                return (
                    await handler(
                        CallToolRequest(
                            method="tools/call",
                            params=CallToolRequestParams(
                                name="execute_query", arguments=args
                            ),
                        )
                    )
                ).root

        assert (await call(first, {})).isError
        plan = await call(first, {"sql": "SELECT 1"})
        assert not plan.isError
        query_id = plan.structuredContent["id"]
        assert not calls
        assert (await call(second, {"confirmation_id": query_id})).isError
        assert not calls
        result = await call(first, {"confirmation_id": query_id})
        assert not result.isError, result
        assert result.structuredContent["rows"] == [[1], [2]]
        assert result.structuredContent["possibly_truncated"] is True
        assert len(calls) == 1
        assert (await call(first, {"confirmation_id": query_id})).isError
        for fmt in ("csv", "xlsx", "json"):
            assert (
                c.get(result.structuredContent["download_urls"][fmt]).status_code == 200
            )
            path = f"/api/v1/queries/{query_id}/download?format={fmt}"
            assert c.get(path, headers=headers).status_code == 200
            assert (
                c.get(
                    path, headers={"Authorization": f"Bearer {other_token}"}
                ).status_code
                == 404
            )
        import hashlib
        import hmac
        import os
        from urllib.parse import urlsplit

        response = c.post(f"/api/v1/queries/{query_id}/download-links", headers=headers)
        assert response.status_code == 200
        relative = response.json()["csv"]
        assert relative.startswith(f"/api/v1/queries/{query_id}/download-link/csv/")
        assert c.get(relative).status_code == 200
        parts = relative.split("/")
        direct = hmac.new(
            os.environ["DATAAGENT_ENCRYPTION_KEY"].encode(),
            f"{query_id}:csv:{parts[-2]}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert parts[-1] != direct
        parts[-1] = direct
        assert c.get("/".join(parts)).status_code == 404
        link = result.structuredContent["download_urls"]["csv"]
        assert link.startswith("http")
        path = urlsplit(link).path
        with sessions().begin() as db:
            db.add(
                QueryExecution(
                    id="other-query",
                    owner_id=second.id,
                    sql="SELECT 2",
                    status="succeeded",
                    rows='{"columns": ["value"], "data": [[2]]}',
                )
            )
        parts = path.split("/")
        for index, value in (
            (4, "other-query"),
            (-3, "json"),
            (-2, "1"),
            (-2, "invalid"),
            (-1, "0" * 64),
        ):
            changed = parts.copy()
            changed[index] = value
            assert c.get("/".join(changed)).status_code == 404
        assert (
            c.post(
                f"/api/v1/queries/{query_id}/download-links",
                headers={"Authorization": f"Bearer {other_token}"},
            ).status_code
            == 404
        )
        future = query_api.time.time() + 3600
        with monkeypatch.context() as clock:
            clock.setattr(query_api.time, "time", lambda: future)
            assert c.get(link).status_code == 404
        with sessions()() as db:
            assert (
                db.query(QueryAudit).filter_by(kind="download_link_created").count()
                == 6
            )
            assert (
                db.query(QueryAudit).filter_by(kind="download_link_used").count() == 4
            )

        with sessions().begin() as db:
            db.get(QueryExecution, query_id).created_at = datetime.now(UTC) - timedelta(
                days=31
            )
        assert c.get(f"/api/v1/queries/{query_id}", headers=headers).status_code == 404
        assert c.get(link).status_code == 404
        assert (
            c.get(f"/api/v1/queries/{query_id}/download", headers=headers).status_code
            == 404
        )
        expired = await call(first, {"sql": "SELECT 1"})
        with sessions().begin() as db:
            db.get(QueryExecution, expired.structuredContent["id"]).created_at = (
                datetime.now(UTC) - timedelta(minutes=31)
            )
        rejected = await call(
            first, {"confirmation_id": expired.structuredContent["id"]}
        )
        assert rejected.isError and "过期" in rejected.structuredContent["message"]
        assert len(calls) == 1
        with sessions()() as db:
            assert (
                db.query(QueryAudit).filter_by(kind="mcp", status="succeeded").count()
                == 1
            )

    asyncio.run(run())


def test_mcp_and_online_use_same_execution_service(client, monkeypatch):
    import asyncio

    from semantica_dataagent import query_api
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import QueryExecution, Token, sessions
    from semantica_dataagent.mcp_context import bind_mcp_context

    calls = []

    async def service(query_id, body, user, kind="query"):
        calls.append((query_id, user.id, kind))
        with sessions().begin() as db:
            db.get(QueryExecution, query_id).status = "succeeded"
        return {"row_count": 0}

    monkeypatch.setattr(query_api, "_execute_with_audit", service)
    _, headers = client
    with sessions().begin() as db:
        token = db.query(Token).filter_by(role="admin").first()
        requester = Requester(
            id=token.id, role=token.role, token=headers["Authorization"][7:]
        )
        db.add_all(
            [
                QueryExecution(
                    id="local", owner_id=token.id, sql="SELECT 1", status="planned"
                ),
                QueryExecution(
                    id="online",
                    owner_id=token.id,
                    sql="SELECT 1",
                    status="planned",
                    conversation_id="conversation",
                    turn_id=1,
                ),
            ]
        )

    async def run():
        with bind_mcp_context(requester):
            await query_api.execute_query(confirmation_id="local")
        await query_api.execute_for_turn("online", requester, "conversation", 1)

    asyncio.run(run())
    assert calls == [("local", requester.id, "mcp"), ("online", requester.id, "query")]
