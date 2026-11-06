from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


def test_authenticated_user_reads_configured_assistant_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings",
            headers=headers,
            json={
                "values": {"assistant_agent": "claude", "assistant_model": "model-a"}
            },
        )
        assert response.status_code == 200
        response = client.get("/api/v1/assistant-defaults", headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "assistant_agent": "claude",
            "assistant_provider_id": None,
            "assistant_model": "model-a",
            "assistant_agent_profiles": None,
        }
        assert client.get("/api/v1/assistant-defaults").status_code == 401


def test_agent_profiles_validate_and_round_trip(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    headers = {"Authorization": f"Bearer {initialize()}"}
    profiles = {
        "claude": {"providers": ["p1"], "default_provider": "p1", "default_model": "m1"}
    }
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"assistant_agent_profiles": json.dumps(profiles)}},
        )
        assert response.status_code == 200
        assert (
            json.loads(
                client.get("/api/v1/assistant-defaults", headers=headers).json()[
                    "assistant_agent_profiles"
                ]
            )
            == profiles
        )
        response = client.put(
            "/api/v1/settings",
            headers=headers,
            json={
                "values": {
                    "assistant_agent_profiles": '{"claude": {"providers": "bad"}}'
                }
            },
        )
        assert response.status_code == 422
        assert (
            json.loads(
                client.get("/api/v1/assistant-defaults", headers=headers).json()[
                    "assistant_agent_profiles"
                ]
            )
            == profiles
        )
