"""验证上传去抖自动增量构建、源状态与开关 (#186)."""

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


def _settings(ctx, values):
    response = _request(ctx, "PUT", "/api/v1/settings", json={"values": values})
    ctx.require(response.status_code == 200, response.text[:300])


def _upload(ctx, category, files, *, project="default", token=None):
    return _request(
        ctx,
        "POST",
        "/api/v1/source/upload",
        project=project,
        token=token,
        json={
            "category": category,
            "files": [
                {"path": path, "content_base64": _payload(text)}
                for path, text in files.items()
            ],
        },
    )


def _status(ctx, *, project="default", token=None):
    response = _request(
        ctx,
        "GET",
        f"/api/v1/projects/{project}/source-status",
        project=project,
        token=token,
    )
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _builds(ctx, *, project="default", token=None):
    response = _request(ctx, "GET", "/api/v1/builds", project=project, token=token)
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _upload_builds(ctx, *, project="default"):
    return [
        row
        for row in _builds(ctx, project=project)
        if row["triggered_by"] == "upload" and row["build_type"] == "incremental"
    ]


def _wait(predicate, ctx, message):
    deadline = time.monotonic() + ctx.config.build_timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(min(0.5, ctx.config.poll_interval))
    raise AssertionError(f"{message}; last={last!r}")


def _wait_effective(ctx, *, project="default", token=None, minimum=0):
    def check():
        rows = _upload_builds(ctx, project=project) if project == "default" else []
        if len(rows) < minimum:
            return None
        row = _status(ctx, project=project, token=token)
        return row if row["state"] == "effective" else None

    return _wait(check, ctx, "source did not become effective")


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


def _create_token(ctx, name, role, projects):
    response = _request(
        ctx,
        "POST",
        "/api/v1/tokens",
        json={"name": name, "role": role, "projects": projects},
    )
    ctx.require(response.status_code == 201, response.text[:300])
    return response.json()["token"]


def check_upload_debounce_status_and_switch(ctx):
    token = ctx.generation.replace("_", "")
    category = "issue186"
    marker1 = f"issue186first{token}"
    marker2 = f"issue186second{token}"
    marker3 = f"issue186third{token}"
    marker4 = f"issue186disabled{token}"

    _settings(
        ctx,
        {
            "auto_build_debounce_seconds": "1",
            "embedding_url": ctx.config.embedding_url,
            "embedding_model": ctx.config.embedding_model,
        },
    )
    assert _status(ctx)["auto_build"] is True
    assert _upload_builds(ctx) == []

    started = time.monotonic()
    for name, text in (
        ("a.md", marker1),
        ("b.md", marker2),
        ("c.md", marker3),
    ):
        response = _upload(ctx, category, {name: text})
        ctx.require(response.status_code == 200, response.text[:300])
    uploaded = time.monotonic()
    pending = _status(ctx)
    ctx.require(pending["state"] == "pending", f"上传后状态应为 pending: {pending}")
    ctx.require(
        pending["due_in_seconds"] is not None and 0 <= pending["due_in_seconds"] <= 3,
        f"倒计时异常: {pending}",
    )

    queued_at = _wait(
        lambda: _upload_builds(ctx) or None,
        ctx,
        "去抖后没有产生上传触发的增量构建",
    )
    build_id = queued_at[0]["id"]
    seen_queued = time.monotonic()
    running_at = _wait(
        lambda: (
            time.monotonic()
            if _request(
                ctx, "GET", f"/api/v1/builds/{build_id}", project="default"
            ).json()["status"]
            == "running"
            else None
        ),
        ctx,
        "构建没有开始运行",
    )
    finished = _wait(
        lambda: (
            time.monotonic()
            if _request(
                ctx, "GET", f"/api/v1/builds/{build_id}", project="default"
            ).json()["status"]
            in {"succeeded", "failed", "cancelled"}
            else None
        ),
        ctx,
        "构建没有结束",
    )
    detail = _request(
        ctx, "GET", f"/api/v1/builds/{build_id}", project="default"
    ).json()
    ctx.require(detail["status"] == "succeeded", f"构建失败: {detail.get('error')}")
    effective = _wait_effective(ctx, minimum=1)
    effective_at = time.monotonic()
    ctx.require(effective["last_build_id"] == build_id, effective)
    ctx.require(effective["source_commit"] == detail["source_commit"], effective)

    searchable_at = _wait(
        lambda: time.monotonic() if _contains(_search(ctx, marker1), marker1) else None,
        ctx,
        "构建完成后检索不到新内容",
    )
    ctx.require(
        len(_upload_builds(ctx)) == 1,
        f"窗口内多次上传产生了多个构建: {_upload_builds(ctx)}",
    )
    ctx.logger.line(
        "ISSUE186_TIMING "
        f"upload_ms={round((uploaded - started) * 1000)} "
        f"upload_to_queued_ms={round((seen_queued - uploaded) * 1000)} "
        f"queued_to_running_ms={round((running_at - seen_queued) * 1000)} "
        f"running_to_finished_ms={round((finished - running_at) * 1000)} "
        f"upload_to_effective_ms={round((effective_at - started) * 1000)} "
        f"effective_to_searchable_ms={round((searchable_at - effective_at) * 1000)} "
        f"upload_to_searchable_ms={round((searchable_at - started) * 1000)}"
    )

    single_marker = f"{marker1}single"
    single_started = time.monotonic()
    response = _upload(ctx, category, {"e.md": f"{single_marker}\n"})
    ctx.require(response.status_code == 200, response.text[:300])
    second = _wait_effective(ctx, minimum=2)
    single_effective = time.monotonic()
    single_searchable = _wait(
        lambda: (
            time.monotonic()
            if _contains(_search(ctx, single_marker), single_marker)
            else None
        ),
        ctx,
        "单文件增量构建完成后检索不到新内容",
    )
    ctx.logger.line(
        "ISSUE186_SINGLE_TIMING "
        f"upload_to_effective_ms={round((single_effective - single_started) * 1000)} "
        f"effective_to_searchable_ms={round((single_searchable - single_effective) * 1000)} "
        f"upload_to_searchable_ms={round((single_searchable - single_started) * 1000)}"
    )
    ctx.require(len(_upload_builds(ctx)) == 2, _upload_builds(ctx))
    ctx.require(second["last_build_id"] != build_id, second)

    response = _request(
        ctx, "PUT", "/api/v1/projects/default/auto-build", json={"enabled": False}
    )
    ctx.require(response.status_code == 200, response.text[:300])
    ctx.require(response.json() == {"enabled": False}, response.text)
    before_disabled = len(_upload_builds(ctx))
    assert _upload(ctx, category, {"off.md": f"{marker4}\n"}).status_code == 200
    time.sleep(2.5)
    ctx.require(
        len(_upload_builds(ctx)) == before_disabled,
        "关闭自动构建后上传仍然触发了构建",
    )
    disabled_status = _status(ctx)
    ctx.require(disabled_status["auto_build"] is False, disabled_status)

    response = _request(
        ctx, "PUT", "/api/v1/projects/default/auto-build", json={"enabled": True}
    )
    ctx.require(response.status_code == 200, response.text[:300])
    ctx.require(_status(ctx)["auto_build"] is True, "重新打开后 auto_build 应为 True")


def check_permissions_and_cross_project_isolation(ctx):
    project = _request(
        ctx,
        "POST",
        "/api/v1/projects",
        json={"name": f"issue186-{ctx.generation[-6:]}", "type": "document"},
    )
    ctx.require(project.status_code == 201, project.text[:300])
    project_id = project.json()["id"]
    ctx.require(_builds(ctx, project=project_id) == [], "新项目不应有构建")
    assert _status(ctx, project=project_id)["state"] == "unknown"

    member = _create_token(
        ctx,
        f"issue186-member-{ctx.generation[-6:]}",
        "user",
        [{"project_id": "default", "role": "member"}],
    )
    outsider = _create_token(
        ctx, f"issue186-outsider-{ctx.generation[-6:]}", "user", []
    )
    project_admin = _create_token(
        ctx,
        f"issue186-admin-{ctx.generation[-6:]}",
        "user",
        [{"project_id": "default", "role": "admin"}],
    )
    ctx.require(
        _status(ctx, token=member)["state"]
        in {
            "pending",
            "building",
            "effective",
            "failed",
            "unknown",
        },
        "成员不能读取状态",
    )
    ctx.require(
        _request(
            ctx,
            "PUT",
            "/api/v1/projects/default/auto-build",
            token=member,
            json={"enabled": True},
        ).status_code
        == 403,
        "普通成员不应能修改自动构建",
    )
    ctx.require(
        _request(
            ctx,
            "GET",
            "/api/v1/projects/default/source-status",
            token=outsider,
        ).status_code
        == 404,
        "非成员读取状态应返回 404",
    )
    ctx.require(
        _request(
            ctx,
            "PUT",
            "/api/v1/projects/default/auto-build",
            token=project_admin,
            json={"enabled": True},
        ).status_code
        == 200,
        "项目管理员应能修改自动构建",
    )
    default_builds = _upload_builds(ctx)
    marker = f"issue186cross{ctx.generation[-6:]}"
    response = _upload(
        ctx, "issue186cross", {f"{marker}.md": marker}, project=project_id
    )
    ctx.require(response.status_code == 200, response.text[:300])
    ctx.require(
        _status(ctx, project=project_id)["state"] in {"pending", "building"},
        "上传到新项目后没有待构建或构建中状态",
    )
    ctx.require(
        _upload_builds(ctx) == default_builds,
        "上传到新项目不应触发默认项目构建",
    )
