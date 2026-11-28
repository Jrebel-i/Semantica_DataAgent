import base64
import io
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_historical_tickets import _configure


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    from semantica_dataagent.app import app
    from semantica_dataagent.project_api import create_project_record
    from semantica_dataagent.project_context import project_source_directory

    token = _configure(tmp_path, monkeypatch)
    project = create_project_record("Leak gate", "warehouse")
    root = project_source_directory(project.id)
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        client.params = {"project": project.id}
        client.put(f"/api/v1/projects/{project.id}/auto-build", json={"enabled": False})
        res = client.post(
            "/api/v1/eval-questions",
            json={
                "id": "bench-1",
                "question": "保留基准问题：昨日支付单量",
                "caliber": "按 paid_time 统计",
                "reference_sql": "SELECT COUNT(*) FROM dm.dm_paid_orders",
                "dialect": "doris",
                "split": "holdout",
                "answer_confirmed": True,
            },
        )
        assert res.status_code == 201, res.text
        yield client, root


CONTENT = "# 保留基准问题：昨日支付单量\n\n## 参考 SQL\n\n```sql\nSELECT COUNT(*) FROM dm.dm_paid_orders\n```\n"


def test_post_and_patch_content(setup_case):
    c, _root = setup_case
    res = c.post(
        "/api/v1/historical-tickets",
        json={"title": "不同标题", "content": "# 保留基准问题：昨日支付单量\n"},
    )
    assert res.status_code == 409, res.text
    assert (
        c.post(
            "/api/v1/historical-tickets", json={"title": "", "content": CONTENT}
        ).status_code
        == 422
    )
    created = c.post(
        "/api/v1/historical-tickets", json={"title": "正常工单", "slug": "normal"}
    )
    assert created.status_code == 201, created.text
    assert (
        c.patch(
            "/api/v1/historical-tickets/normal", json={"content": CONTENT}
        ).status_code
        == 409
    )


def test_approval_scoped_to_tickets(setup_case):
    c, root = setup_case
    for category, status in [("historical-tickets", 409), ("sales", 200)]:
        target = f"document:knowledge/{category}/example.md"
        proposal = c.post(
            "/api/v1/corrections",
            json={
                "target": target,
                "target_kind": "document",
                "patch": CONTENT,
                "reason": "补充业务知识",
            },
        )
        assert proposal.status_code == 201, proposal.text
        pid = proposal.json()["id"]
        result = c.post(f"/api/v1/corrections/{pid}/approve")
        assert result.status_code == status, result.text
        assert (root / f"knowledge/{category}/example.md").exists() == (status == 200)
        if status == 409:
            assert c.get(f"/api/v1/corrections/{pid}").json()["status"] == "pending"


@pytest.mark.parametrize("archive", [False, True])
def test_upload_atomic_leak_rejection(setup_case, monkeypatch, archive):
    from semantica_dataagent import auto_build

    c, root = setup_case
    calls = []
    monkeypatch.setattr(
        auto_build, "request_upload_build", lambda *args: calls.append(args)
    )
    before = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    data = (
        b"# Different question\n\n```sql\nSELECT COUNT(*) FROM dm.dm_paid_orders\n```\n"
    )
    body = {"target": "knowledge/historical-tickets"}
    if archive:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("safe.md", "# 无关内容")
            z.writestr("leak.md", data)
        body["archive_base64"] = base64.b64encode(buf.getvalue()).decode()
    else:
        body["files"] = [
            {"path": "leak.md", "content_base64": base64.b64encode(data).decode()}
        ]
    result = c.post("/api/v1/source/upload", json=body)
    assert result.status_code == 409, result.text
    assert not (root / "knowledge/historical-tickets/leak.md").exists()
    assert not (root / "knowledge/historical-tickets/safe.md").exists()
    assert (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        == before
    )
    assert not calls


def test_upload_headingless_ticket_uses_filename(setup_case):
    c, root = setup_case
    name = "保留基准问题：昨日支付单量.md"
    response = c.post(
        "/api/v1/source/upload",
        json={
            "target": "knowledge/historical-tickets",
            "files": [
                {
                    "path": name,
                    "content_base64": base64.b64encode(
                        "无 SQL 的工单".encode()
                    ).decode(),
                }
            ],
        },
    )
    assert response.status_code == 409, response.text
    assert not (root / "knowledge/historical-tickets" / name).exists()


def test_headingless_post_and_approval_use_filename(setup_case):
    c, root = setup_case
    name = "保留基准问题：昨日支付单量"
    content = "无 SQL 的工单"
    result = c.post(
        "/api/v1/historical-tickets",
        json={"title": "其他工单", "slug": name, "content": content},
    )
    assert result.status_code == 409, result.text
    proposal = c.post(
        "/api/v1/corrections",
        json={
            "target": f"document:knowledge/historical-tickets/{name}.md",
            "target_kind": "document",
            "patch": content,
            "reason": "补充业务知识",
        },
    )
    assert proposal.status_code == 201, proposal.text
    result = c.post(f"/api/v1/corrections/{proposal.json()['id']}/approve")
    assert result.status_code == 409, result.text
    assert not (root / "knowledge/historical-tickets" / f"{name}.md").exists()
