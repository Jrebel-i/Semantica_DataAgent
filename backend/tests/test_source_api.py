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


def test_failed_git_commit_restores_whole_category_for_subfolder_replace(
    source_client,
):
    c, h, root = source_client
    assert (
        upload(
            c,
            h,
            "knowledge/cat",
            {"a.md": "original", "sub/old.md": "old", "other/b.md": "b"},
            replace=True,
        ).status_code
        == 200
    )
    (root / ".git/index.lock").write_text("locked")
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "category": "cat",
            "subfolder": "sub",
            "files": [{"path": "a.md", "content_base64": payload("changed")}],
            "replace": True,
        },
    )
    assert response.status_code == 422
    assert (root / "knowledge/cat/a.md").read_text() == "original"
    assert (root / "knowledge/cat/sub/old.md").read_text() == "old"
    assert (root / "knowledge/cat/other/b.md").read_text() == "b"


def test_replace_with_subfolder_deletes_unlisted_category_files(source_client):
    c, h, root = source_client
    assert (
        upload(
            c,
            h,
            "knowledge/cat",
            {"sub/old.md": "old", "other/b.md": "b"},
            replace=True,
        ).status_code
        == 200
    )
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "category": "cat",
            "subfolder": "sub",
            "files": [{"path": "new.md", "content_base64": payload("new")}],
            "replace": True,
        },
    )
    assert response.status_code == 200, response.text
    assert (root / "knowledge/cat/sub/new.md").read_text() == "new"
    assert not (root / "knowledge/cat/sub/old.md").exists()
    assert not (root / "knowledge/cat/other/b.md").exists()


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


def preview(c, h, files=None, archive_base64=None, **kwargs):
    body = {**kwargs}
    if files is not None:
        body["files"] = [
            {"path": p, "content_base64": payload(v)} for p, v in files.items()
        ]
    if archive_base64 is not None:
        body["archive_base64"] = archive_base64
    return c.post("/api/v1/source/upload/preview", headers=h, json=body)


def snapshot(root):
    files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return files, head


def test_preview_classifies_added_overwritten_unchanged_and_deleted(source_client):
    c, h, _ = source_client
    assert (
        upload(
            c,
            h,
            "knowledge/cat",
            {
                "same.md": "same\n",
                "change.md": "old\n",
                "sub/nested.md": "nested\n",
                "中文 文件.md": "中文\n",
                "gone.md": "gone\n",
            },
        ).status_code
        == 200
    )
    result = preview(
        c,
        h,
        target="knowledge/cat",
        files={
            "same.md": "same\n",
            "change.md": "new\n",
            "add.md": "add\n",
            "sub/nested.md": "nested\n",
            "中文 文件.md": "中文\n",
        },
    )
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["category"] == "cat"
    assert data["target"] == "knowledge/cat"
    assert data["added"] == ["knowledge/cat/add.md"]
    assert data["overwritten"] == ["knowledge/cat/change.md"]
    assert data["unchanged"] == [
        "knowledge/cat/same.md",
        "knowledge/cat/sub/nested.md",
        "knowledge/cat/中文 文件.md",
    ]
    assert data["deleted"] == []
    assert data["counts"] == {
        "added": 1,
        "overwritten": 1,
        "unchanged": 3,
        "deleted": 0,
    }
    assert data["truncated"] is False

    replaced = preview(
        c,
        h,
        target="knowledge/cat",
        files={"same.md": "same\n", "change.md": "new\n"},
        replace=True,
    )
    assert replaced.status_code == 200, replaced.text
    replaced_data = replaced.json()
    assert replaced_data["deleted"] == [
        "knowledge/cat/gone.md",
        "knowledge/cat/sub/nested.md",
        "knowledge/cat/中文 文件.md",
    ]
    assert replaced_data["counts"] == {
        "added": 0,
        "overwritten": 1,
        "unchanged": 1,
        "deleted": 3,
    }


def test_preview_has_no_side_effects(source_client):
    c, h, root = source_client
    assert upload(c, h, "knowledge/cat", {"a.md": "old\n"}).status_code == 200
    before = snapshot(root)
    response = preview(
        c, h, target="knowledge/cat", files={"a.md": "new\n", "b.md": "new\n"}
    )
    assert response.status_code == 200, response.text
    assert snapshot(root) == before
    assert (root / "knowledge/cat/a.md").read_text() == "old\n"
    assert not (root / "knowledge/cat/b.md").exists()

    # A preview before the source repository exists must not create it.
    (root / "knowledge/cat").rename(root / "moved")
    moved = snapshot(root)
    fresh = preview(c, h, target="knowledge/cat", files={"a.md": "new\n"})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["added"] == ["knowledge/cat/a.md"]
    assert snapshot(root) == moved


def test_supplement_upload_keeps_files_and_overwrites_with_git_history(source_client):
    c, h, root = source_client
    first = upload(c, h, "knowledge/cat", {"a.md": "old\n", "keep.md": "keep\n"})
    assert first.status_code == 200, first.text
    second = upload(c, h, "knowledge/cat", {"a.md": "new\n"})
    assert second.status_code == 200, second.text
    assert (root / "knowledge/cat/a.md").read_text() == "new\n"
    assert (root / "knowledge/cat/keep.md").read_text() == "keep\n"
    assert c.get("/api/v1/source/files", headers=h).json() == [
        "knowledge/cat/a.md",
        "knowledge/cat/keep.md",
    ]
    message = subprocess.check_output(
        ["git", "-C", str(root), "log", "-1", "--format=%s"], text=True
    ).strip()
    assert "cat" in message and "新增 0" in message and "覆盖 1" in message


def test_replace_mode_deletes_omitted_files_on_category_upload(source_client):
    c, h, root = source_client
    assert (
        upload(c, h, "knowledge/cat", {"a.md": "old", "gone.md": "x"}).status_code
        == 200
    )
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "category": "cat",
            "files": [{"path": "a.md", "content_base64": payload("new")}],
            "replace": True,
        },
    )
    assert response.status_code == 200, response.text
    assert (root / "knowledge/cat/a.md").read_text() == "new"
    assert not (root / "knowledge/cat/gone.md").exists()


def test_category_upload_targets_category_and_subfolder(source_client):
    c, h, root = source_client
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "category": "知识 类",
            "subfolder": "子 目录",
            "files": [{"path": "a.md", "content_base64": payload("a")}],
        },
    )
    assert response.status_code == 200, response.text
    assert (root / "knowledge/知识 类/子 目录/a.md").read_text() == "a"

    preview_response = preview(
        c,
        h,
        category="知识 类",
        subfolder="子 目录",
        files={"a.md": "a", "b.md": "b"},
    )
    assert preview_response.status_code == 200, preview_response.text
    assert preview_response.json()["target"] == "knowledge/知识 类/子 目录"
    assert preview_response.json()["added"] == ["knowledge/知识 类/子 目录/b.md"]
    assert preview_response.json()["unchanged"] == ["knowledge/知识 类/子 目录/a.md"]


def test_category_and_target_are_mutually_exclusive(source_client):
    c, h, _ = source_client
    files = {"a.md": "a"}
    assert (
        preview(c, h, target="knowledge/cat", category="cat", files=files).status_code
        == 422
    )
    assert preview(c, h, files=files).status_code == 422
    assert preview(c, h, subfolder="sub", files=files).status_code == 422
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            "target": "knowledge/cat",
            "category": "cat",
            "files": [{"path": "a.md", "content_base64": payload("a")}],
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "kwargs",
    [
        {"category": ".."},
        {"category": "/absolute"},
        {"category": "a/b"},
        {"category": ".hidden"},
        {"category": "bad\x01name"},
        {"category": ""},
        {"category": "cat", "subfolder": ".."},
        {"category": "cat", "subfolder": "/absolute"},
        {"category": "cat", "subfolder": "a//b"},
        {"category": "cat", "subfolder": ".hidden"},
        {"category": "cat", "subfolder": "a/../b"},
        {"category": "cat", "subfolder": "a/.hidden"},
        {"category": "cat", "subfolder": "bad\x02name"},
    ],
)
def test_category_and_subfolder_path_safety(source_client, kwargs):
    c, h, root = source_client
    response = preview(c, h, files={"a.md": "a"}, **kwargs)
    assert response.status_code == 422, response.text
    upload_response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={
            **kwargs,
            "files": [{"path": "a.md", "content_base64": payload("a")}],
        },
    )
    assert upload_response.status_code == 422
    assert not (root / "knowledge").exists()


def test_category_zip_preview_and_upload_strip_category_folder(source_client):
    c, h, root = source_client
    data = archive("cat/a.md", b"zip-new")
    result = preview(c, h, category="cat", archive_base64=data)
    assert result.status_code == 200, result.text
    assert result.json()["added"] == ["knowledge/cat/a.md"]
    response = c.post(
        "/api/v1/source/upload",
        headers=h,
        json={"category": "cat", "archive_base64": data},
    )
    assert response.status_code == 200, response.text
    assert (root / "knowledge/cat/a.md").read_text() == "zip-new"
    assert not (root / "knowledge/cat/cat").exists()


def test_preview_truncates_long_lists_but_keeps_counts(source_client):
    c, h, _ = source_client
    files = {f"file-{index:03d}.md": f"{index}\n" for index in range(205)}
    result = preview(c, h, target="knowledge/cat", files=files)
    assert result.status_code == 200, result.text
    data = result.json()
    assert len(data["added"]) == 200
    assert data["counts"]["added"] == 205
    assert data["truncated"] is True


def test_preview_enforces_file_and_size_limits(source_client):
    c, h, _ = source_client
    too_many = {f"{index}.md": "x" for index in range(1001)}
    assert preview(c, h, target="knowledge/cat", files=too_many).status_code == 422
    too_large = {"big.md": "x" * (20 * 1024 * 1024 + 1)}
    assert preview(c, h, target="knowledge/cat", files=too_large).status_code == 422


def project_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATAAGENT_PROJECTS_ROOT", str(tmp_path / "projects"))
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


def create_project(c, admin, name):
    response = c.post(
        "/api/v1/projects",
        headers=admin,
        json={"name": name, "type": "document"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_token(c, admin, name, projects):
    response = c.post(
        "/api/v1/tokens",
        headers=admin,
        json={"name": name, "role": "user", "projects": projects},
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_project_admin_can_preview_and_upload_members_cannot(tmp_path, monkeypatch):
    for c, admin, _ in project_client(tmp_path, monkeypatch):
        project = create_project(c, admin, "Upload Docs")
        project_id = project["id"]
        pa = create_token(c, admin, "pa", [{"project_id": project_id, "role": "admin"}])
        member = create_token(
            c, admin, "member", [{"project_id": project_id, "role": "member"}]
        )
        outsider = create_token(c, admin, "outsider", [])
        body = {
            "category": "cat",
            "files": [{"path": "a.md", "content_base64": payload("a")}],
        }
        preview_response = c.post(
            "/api/v1/source/upload/preview",
            headers=auth(pa["token"]),
            params={"project": project_id},
            json=body,
        )
        assert preview_response.status_code == 200, preview_response.text
        assert (
            c.post(
                "/api/v1/source/upload",
                headers=auth(pa["token"]),
                params={"project": project_id},
                json=body,
            ).status_code
            == 200
        )
        for token, expected in ((member["token"], 403), (outsider["token"], 404)):
            assert (
                c.post(
                    "/api/v1/source/upload/preview",
                    headers=auth(token),
                    params={"project": project_id},
                    json=body,
                ).status_code
                == expected
            )
            assert (
                c.post(
                    "/api/v1/source/upload",
                    headers=auth(token),
                    params={"project": project_id},
                    json=body,
                ).status_code
                == expected
            )


def test_upload_to_project_a_does_not_touch_project_b(tmp_path, monkeypatch):
    for c, admin, default_root in project_client(tmp_path, monkeypatch):
        project_a = create_project(c, admin, "Project A")
        project_b = create_project(c, admin, "Project B")
        pa = create_token(
            c, admin, "pa", [{"project_id": project_a["id"], "role": "admin"}]
        )
        pb = create_token(
            c, admin, "pb", [{"project_id": project_b["id"], "role": "admin"}]
        )
        assert (
            c.post(
                "/api/v1/source/upload",
                headers=auth(pa["token"]),
                params={"project": project_a["id"]},
                json={
                    "category": "cat",
                    "files": [{"path": "a.md", "content_base64": payload("A")}],
                },
            ).status_code
            == 200
        )
        files_b = c.get(
            "/api/v1/source/files",
            headers=auth(pb["token"]),
            params={"project": project_b["id"]},
        )
        assert files_b.status_code == 200
        assert files_b.json() == []
        files_a = c.get(
            "/api/v1/source/files",
            headers=auth(pa["token"]),
            params={"project": project_a["id"]},
        )
        assert files_a.json() == ["knowledge/cat/a.md"]
        assert not (default_root / "knowledge/cat/a.md").exists()


def test_legacy_target_upload_and_preview_still_work(source_client):
    c, h, root = source_client
    result = preview(c, h, target="knowledge/cat", files={"a.md": "a"})
    assert result.status_code == 200, result.text
    assert result.json()["category"] == "cat"
    assert result.json()["target"] == "knowledge/cat"
    assert result.json()["added"] == ["knowledge/cat/a.md"]
    assert upload(c, h, "knowledge/cat", {"a.md": "a"}).status_code == 200
    assert (root / "knowledge/cat/a.md").read_text() == "a"
