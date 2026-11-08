"""通过 REST 与真实构建验证上传预览、补充模式和知识类上传 (#185)."""

from __future__ import annotations

import base64
import time


def _payload(text):
    return base64.b64encode(text.encode()).decode()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _request(ctx, method, path, *, project="default", token=None, **kwargs):
    params = dict(kwargs.pop("params", {}))
    params["project"] = project
    headers = _headers(token) if token else None
    return ctx.rest.request(method, path, params=params, headers=headers, **kwargs)


def _preview(ctx, body, *, project="default", token=None):
    return _request(
        ctx,
        "POST",
        "/api/v1/source/upload/preview",
        project=project,
        token=token,
        json=body,
    )


def _upload(ctx, body, *, project="default", token=None):
    return _request(
        ctx,
        "POST",
        "/api/v1/source/upload",
        project=project,
        token=token,
        json=body,
    )


def _files(ctx, *, project="default", token=None):
    response = _request(
        ctx, "GET", "/api/v1/source/files", project=project, token=token
    )
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _build(ctx, project="default"):
    response = _request(
        ctx,
        "POST",
        "/api/v1/builds",
        project=project,
        json={"build_type": "incremental"},
    )
    ctx.require(response.status_code == 202, response.text[:300])
    build_id = response.json()["id"]
    deadline = time.monotonic() + ctx.config.build_timeout
    while time.monotonic() < deadline:
        detail = _request(ctx, "GET", f"/api/v1/builds/{build_id}", project=project)
        ctx.require(detail.status_code == 200, detail.text[:300])
        row = detail.json()
        if row.get("status") in {"succeeded", "failed", "cancelled"}:
            ctx.require(
                row["status"] == "succeeded",
                f"增量构建失败: {row.get('status')} {row.get('error')}",
            )
            return row
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"增量构建 {build_id} 超时")


def _search(ctx, query, *, project="default", token=None):
    response = _request(
        ctx,
        "GET",
        "/api/v1/search",
        project=project,
        token=token,
        params={"query": query, "scope": "docs", "top_k": 10},
    )
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _contains(hits, marker):
    return any(
        marker in str(row.get("content", "")) or marker in str(row.get("name", ""))
        for row in hits
    )


def check_preview_supplement_and_overwrite(ctx):
    category = "issue185"
    token = ctx.generation.replace("_", "")
    first = f"issue185markerfirst{token}"
    second = f"issue185markersecond{token}"
    base = {"category": category}

    preview = _preview(
        ctx,
        {
            **base,
            "files": [
                {"path": "supplement.md", "content_base64": _payload(first)},
                {"path": "keep.md", "content_base64": _payload("keep-me")},
            ],
        },
    )
    ctx.require(preview.status_code == 200, preview.text[:300])
    data = preview.json()
    ctx.require(
        data["counts"]
        == {
            "added": 2,
            "overwritten": 0,
            "unchanged": 0,
            "deleted": 0,
        },
        f"首次预览计数错误: {data['counts']}",
    )
    ctx.require(data["truncated"] is False, data)
    ctx.require("knowledge/issue185/supplement.md" in data["added"], data["added"])
    ctx.require(
        "knowledge/issue185/supplement.md" not in _files(ctx),
        "预览不应写入源文件",
    )

    upload = _upload(
        ctx,
        {
            **base,
            "files": [
                {"path": "supplement.md", "content_base64": _payload(first)},
                {"path": "keep.md", "content_base64": _payload("keep-me")},
            ],
        },
    )
    ctx.require(upload.status_code == 200, upload.text[:300])
    listed = _files(ctx)
    ctx.require(
        {"knowledge/issue185/supplement.md", "knowledge/issue185/keep.md"}
        <= set(listed),
        listed,
    )

    _build(ctx)
    hits = _search(ctx, first)
    ctx.require(_contains(hits, first), f"构建后检索不到补充文件: {hits!r}")
    ctx.require(
        _contains(_search(ctx, "支付销售额"), "支付销售额"),
        "原有知识文档在补充上传后检索不到",
    )
    ctx.require(
        "knowledge/metrics/definitions/sales.md" in _files(ctx),
        "原有源文件被补充上传删除",
    )

    overwrite = _preview(
        ctx,
        {
            **base,
            "files": [{"path": "supplement.md", "content_base64": _payload(second)}],
        },
    )
    ctx.require(overwrite.status_code == 200, overwrite.text[:300])
    overwritten = overwrite.json()
    ctx.require(
        overwritten["counts"]
        == {
            "added": 0,
            "overwritten": 1,
            "unchanged": 0,
            "deleted": 0,
        },
        f"覆盖预览计数错误: {overwritten['counts']}",
    )
    ctx.require(
        overwritten["overwritten"] == ["knowledge/issue185/supplement.md"],
        overwritten,
    )

    replaced = _upload(
        ctx,
        {
            **base,
            "files": [{"path": "supplement.md", "content_base64": _payload(second)}],
        },
    )
    ctx.require(replaced.status_code == 200, replaced.text[:300])
    ctx.require(
        "knowledge/issue185/keep.md" in _files(ctx),
        "补充模式误删了未上传的 keep.md",
    )
    _build(ctx)
    new_hits = _search(ctx, second)
    ctx.require(_contains(new_hits, second), f"覆盖后检索不到新内容: {new_hits!r}")
    old_hits = _search(ctx, first)
    ctx.require(
        not _contains(old_hits, first),
        f"覆盖后仍能检索到旧内容: {old_hits!r}",
    )
    return {
        "category": category,
        "preview_first": data["counts"],
        "preview_overwrite": overwritten["counts"],
        "new_hits": len(new_hits),
    }


def check_preview_and_upload_permissions_and_isolation(ctx):
    suffix = ctx.generation[-8:]
    create = _request(
        ctx,
        "POST",
        "/api/v1/projects",
        json={"name": f"Issue 185 Docs {suffix}", "type": "document"},
    )
    ctx.require(create.status_code == 201, create.text[:300])
    project_id = create.json()["id"]

    def token(name, projects):
        response = _request(
            ctx,
            "POST",
            "/api/v1/tokens",
            json={"name": name, "role": "user", "projects": projects},
        )
        ctx.require(response.status_code == 201, response.text[:300])
        return response.json()["token"]

    project_admin = token(
        f"issue185-pa-{suffix}", [{"project_id": project_id, "role": "admin"}]
    )
    member = token(
        f"issue185-member-{suffix}", [{"project_id": project_id, "role": "member"}]
    )
    outsider = token(f"issue185-out-{suffix}", [])
    body = {
        "category": "tenant",
        "files": [{"path": "a.md", "content_base64": _payload("project-admin")}],
    }

    preview = _preview(ctx, body, project=project_id, token=project_admin)
    ctx.require(preview.status_code == 200, preview.text[:300])
    upload = _upload(ctx, body, project=project_id, token=project_admin)
    ctx.require(upload.status_code == 200, upload.text[:300])
    ctx.require(
        "knowledge/tenant/a.md" in _files(ctx, project=project_id, token=project_admin),
        "项目管理员上传后文件不在该项目源目录",
    )
    for who, expected in ((member, 403), (outsider, 404)):
        ctx.require(
            _preview(ctx, body, project=project_id, token=who).status_code == expected,
            f"预览权限应为 {expected}",
        )
        ctx.require(
            _upload(ctx, body, project=project_id, token=who).status_code == expected,
            f"上传权限应为 {expected}",
        )
    ctx.require(
        "knowledge/tenant/a.md" not in _files(ctx),
        "往非默认项目上传写进了默认项目源目录",
    )
    return {
        "project_id": project_id,
        "roles": {"project_admin": 200, "member": 403, "outsider": 404},
    }
