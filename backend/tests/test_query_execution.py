"""REST contract for query execution and caller-owned DTB credentials."""

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
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
                        "data": {"progress": 1, "columns": ["v"], "data": [[1], [2]]},
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
            "select * from shop.orders where dt = '2025' or 1=1",
        ]:
            response = c.post(
                "/api/v1/queries/plan", headers=headers, json={"sql": sql}
            )
            assert response.status_code == 422, response.text
        response = c.post(
            "/api/v1/queries/plan",
            headers=headers,
            json={"sql": "select * from shop.orders where dt = '2025'"},
        )
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["estimated_rows"] is None
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
        response = c.post(path + "/execute", headers=headers, json={"limit": 1})
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [[1]]
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
