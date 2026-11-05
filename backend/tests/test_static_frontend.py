from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_frontend_serves_assets_without_hiding_api(tmp_path):
    from semantica_dataagent import app as module

    mount_frontend = getattr(module, "mount_frontend", None)
    assert mount_frontend is not None, "frontend static files must be served"

    (tmp_path / "index.html").write_text("<html>frontend</html>")
    (tmp_path / "asset.js").write_text("console.log('asset')")
    app = FastAPI()

    @app.get("/api/v1/example")
    def example():
        return {"ok": True}

    mount_frontend(app, tmp_path)
    client = TestClient(app)
    assert client.get("/").text == "<html>frontend</html>"
    assert client.get("/asset.js").text == "console.log('asset')"
    assert client.get("/api/v1/example").json() == {"ok": True}
    assert client.get("/missing.js").status_code == 404
    assert (
        client.get("/settings", headers={"Accept": "text/html"}).text
        == "<html>frontend</html>"
    )
    assert (
        client.get("/api/v1/missing", headers={"Accept": "text/html"}).status_code
        == 404
    )
