import base64
import io
import stat
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def source_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.app import app
    from semantica_dataagent.cli import initialize

    token = initialize()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        root = tmp_path / "sources"
        assert (
            client.put(
                "/api/v1/settings",
                headers=headers,
                json={"values": {"source_directory": str(root)}},
            ).status_code
            == 200
        )
        yield client, headers, root


def payload(text):
    return base64.b64encode(text.encode()).decode()


def upload(c, h, target, files, **kwargs):
    return c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "target": target,
            "files": [
                {"path": p, "content_base64": payload(v)} for p, v in files.items()
            ],
            **kwargs,
        },
    )


def test_upload_history_diff_and_replace(source_client):
    c, h, root = source_client
    first = upload(
        c,
        h,
        "knowledge/sales",
        {"a.md": "first\n", "b.md": "delete me\n"},
        replace=True,
    )
    assert first.status_code == 200, first.text
    second = upload(c, h, "knowledge/sales", {"a.md": "second\n"}, replace=True)
    assert second.status_code == 200, second.text
    assert not (root / "knowledge/sales/b.md").exists()
    assert (
        subprocess.check_output(
            ["git", "-C", str(root), "log", "-1", "--format=%an"], text=True
        ).strip()
        == "initial admin"
    )
    assert c.get("/api/v1/source/files", headers=h).json() == ["knowledge/sales/a.md"]
    assert (
        c.get(
            "/api/v1/source/file", headers=h, params={"path": "knowledge/sales/a.md"}
        ).json()["content"]
        == "second\n"
    )
    history = c.get(
        "/api/v1/source/history", headers=h, params={"path": "knowledge/sales/a.md"}
    ).json()
    assert len(history) == 2
    diff = c.get(
        "/api/v1/source/diff",
        headers=h,
        params={
            "path": "knowledge/sales/a.md",
            "before": first.json()["commit"],
            "after": second.json()["commit"],
        },
    ).json()["diff"]
    assert "-first" in diff and "+second" in diff


def archive(name, content=b"x", mode=0):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, content)
    return base64.b64encode(data.getvalue()).decode()


@pytest.mark.parametrize(
    "data",
    [
        archive("../outside"),
        archive("link", mode=stat.S_IFLNK | 0o777),
        archive("huge", b"x" * (20 * 1024 * 1024 + 1)),
    ],
)
def test_reject_unsafe_archives(source_client, data):
    c, h, root = source_client
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={"target": "knowledge/cat", "archive_base64": data},
    )
    assert response.status_code == 422
    assert not (root / "knowledge/cat").exists()


def test_concurrent_uploads_commit_once_each(source_client):
    c, h, root = source_client
    barrier = threading.Barrier(2)

    def send(name):
        barrier.wait()
        return upload(c, h, "warehouse", {name: name})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, ["one.json", "two.json"]))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["commit"] for r in responses}) == 2
    assert (
        subprocess.check_output(
            ["git", "-C", str(root), "rev-list", "--count", "HEAD"], text=True
        ).strip()
        == "2"
    )
    assert (root / "warehouse/one.json").read_text() == "one.json"
    assert (root / "warehouse/two.json").read_text() == "two.json"


def test_paths_auth_and_existing_files(source_client, tmp_path):
    c, h, root = source_client
    root.mkdir()
    (root / "existing.md").write_text("existing")
    assert c.get("/api/v1/source/files").status_code == 401
    assert upload(c, h, "../escape", {"a": "bad"}).status_code == 422
    assert upload(c, h, "knowledge/cat", {"../escape": "bad"}).status_code == 422
    assert upload(c, h, "warehouse", {"new.json": "{}"}).status_code == 200
    assert (
        subprocess.check_output(
            ["git", "-C", str(root), "show", "HEAD:existing.md"], text=True
        )
        == "existing"
    )
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    assert upload(c, h, "link", {"outside.txt": "bad"}).status_code == 422
    assert not (tmp_path / "outside.txt").exists()


def test_archive_file_count_limit(source_client):
    c, h, root = source_client
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for i in range(1001):
            z.writestr(f"{i}.md", "x")
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "target": "knowledge/cat",
            "archive_base64": base64.b64encode(data.getvalue()).decode(),
        },
    )
    assert response.status_code == 422
    assert not (root / "knowledge/cat").exists()


def test_zip_upload_and_unrelated_dirty_file(source_client):
    c, h, root = source_client
    assert (
        upload(
            c, h, "knowledge/cat", {"a.md": "old", "b.md": "original"}, replace=True
        ).status_code
        == 200
    )
    (root / "knowledge/cat/b.md").write_text("uncommitted")
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={"target": "knowledge/cat", "archive_base64": archive("a.md", b"new")},
    )
    assert response.status_code == 200
    assert (root / "knowledge/cat/a.md").read_text() == "new"
    assert (root / "knowledge/cat/b.md").read_text() == "uncommitted"
    assert (
        subprocess.check_output(
            ["git", "-C", str(root), "show", "HEAD:knowledge/cat/b.md"], text=True
        )
        == "original"
    )


def test_reject_invalid_author_without_modification(source_client):
    c, h, root = source_client
    assert upload(c, h, "knowledge/cat", {"a.md": "original"}).status_code == 200
    issued = c.post(
        "/api/v1/tokens", headers=h, json={"name": "Bad <Name>", "role": "admin"}
    ).json()
    bad = {"Authorization": "Bearer " + issued["token"]}
    assert (
        upload(c, bad, "knowledge/cat", {"a.md": "changed"}, replace=True).status_code
        == 422
    )
    assert (root / "knowledge/cat/a.md").read_text() == "original"


def test_replace_category_with_zipped_folder(source_client):
    c, h, root = source_client
    assert (
        upload(c, h, "knowledge/cat", {"removed.md": "old"}, replace=True).status_code
        == 200
    )
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "target": "knowledge/cat",
            "archive_base64": archive("cat/new.md", b"new"),
            "replace": True,
        },
    )
    assert response.status_code == 200
    assert (root / "knowledge/cat/new.md").read_text() == "new"
    assert not (root / "knowledge/cat/removed.md").exists()


def test_failed_git_commit_restores_category(source_client):
    c, h, root = source_client
    assert (
        upload(
            c, h, "knowledge/cat", {"a.md": "original", "b.md": "keep"}, replace=True
        ).status_code
        == 200
    )
    (root / ".git/index.lock").write_text("locked")
    assert (
        upload(c, h, "knowledge/cat", {"a.md": "changed"}, replace=True).status_code
        == 422
    )
    assert (root / "knowledge/cat/a.md").read_text() == "original"
    assert (root / "knowledge/cat/b.md").read_text() == "keep"


def test_unreadable_directory_is_not_an_empty_tree(source_client):
    c, h, root = source_client
    assert upload(c, h, "knowledge/cat", {"a.md": "content"}).status_code == 200
    directory = root / "knowledge/cat"
    directory.chmod(0)
    try:
        assert c.get("/api/v1/source/files", headers=h).status_code == 422
    finally:
        directory.chmod(0o700)


def test_upload_uses_the_actual_admin_token_name(source_client):
    c, headers, root = source_client
    issued = c.post(
        "/api/v1/tokens", headers=headers, json={"name": "Alice", "role": "admin"}
    ).json()
    user = c.post(
        "/api/v1/tokens", headers=headers, json={"name": "Reader", "role": "user"}
    ).json()
    alice_headers = {"Authorization": "Bearer " + issued["token"]}
    user_headers = {"Authorization": "Bearer " + user["token"]}
    assert (
        upload(c, user_headers, "warehouse", {"tables.json": "[]"}).status_code == 403
    )
    assert (
        upload(c, alice_headers, "warehouse", {"tables.json": "[]"}).status_code == 200
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(root), "log", "-1", "--format=%an"], text=True
        ).strip()
        == "Alice"
    )


def test_replace_category_under_configured_markdown_path(source_client):
    c, h, root = source_client
    response = c.put(
        "/api/v1/plugins/source/markdown",
        headers=h,
        json={"enabled": True, "parameters": {"path": "docs"}},
    )
    assert response.status_code == 200
    assert upload(c, h, "docs/cat", {"old.md": "old"}, replace=True).status_code == 200
    assert upload(c, h, "docs/cat", {"new.md": "new"}, replace=True).status_code == 200
    assert not (root / "docs/cat/old.md").exists()
    assert (root / "docs/cat/new.md").read_text() == "new"


def test_binary_source_content_is_available(source_client):
    c, h, _ = source_client
    data = base64.b64encode(b"\x00\xffbinary").decode()
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "target": "warehouse",
            "files": [{"path": "tables.xlsx", "content_base64": data}],
        },
    )
    assert response.status_code == 200
    content = c.get(
        "/api/v1/source/file", headers=h, params={"path": "warehouse/tables.xlsx"}
    )
    assert content.status_code == 200
    assert content.json()["encoding"] == "base64"
    assert content.json()["content"] == data
