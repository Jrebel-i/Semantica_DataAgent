"""未设置去抖窗口时上传先待构建，窗口过后再构建 (#224).

The harness leaves ``auto_build_debounce_seconds`` unset.  A new project must
therefore get the 30 second default window: the upload creates a pending
request with a countdown near 30, and no build starts immediately.  The second
segment shortens the window to 3 seconds and uploads again so the pending
request is extended to the short window; one build must cover both uploads,
finish, become effective, and make both markers searchable.
"""

from __future__ import annotations

import base64
import time

DEFAULT_WINDOW = 30
SHORT_WINDOW = 3


def _payload(text):
    return base64.b64encode(text.encode()).decode()


def _request(ctx, method, path, *, project, **kwargs):
    params = dict(kwargs.pop("params", {}))
    params["project"] = project
    return ctx.rest.request(method, path, params=params, **kwargs)


def _settings(ctx, values):
    response = ctx.rest.put("/api/v1/settings", json={"values": values})
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _create_project(ctx):
    response = ctx.rest.post(
        "/api/v1/projects",
        json={"name": f"issue224-{ctx.generation[-6:]}", "type": "document"},
    )
    ctx.require(response.status_code == 201, response.text[:300])
    return response.json()


def _upload(ctx, project, marker):
    response = _request(
        ctx,
        "POST",
        "/api/v1/source/upload",
        project=project,
        json={
            "category": "issue224",
            "files": [
                {
                    "path": f"{marker}.md",
                    "content_base64": _payload(
                        f"# {marker}\n\n{marker} 是去抖窗口端到端检查的检索标记。\n"
                    ),
                }
            ],
        },
    )
    ctx.require(response.status_code == 200, response.text[:300])


def _builds(ctx, project):
    response = _request(ctx, "GET", "/api/v1/builds", project=project)
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _build(ctx, project, build_id):
    response = _request(ctx, "GET", f"/api/v1/builds/{build_id}", project=project)
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _status(ctx, project):
    response = _request(
        ctx, "GET", f"/api/v1/projects/{project}/source-status", project=project
    )
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _search(ctx, project, marker):
    response = _request(
        ctx,
        "GET",
        "/api/v1/search",
        project=project,
        params={"query": marker, "scope": "docs", "top_k": 10},
    )
    ctx.require(response.status_code == 200, response.text[:300])
    return response.json()


def _contains(hits, marker):
    return any(
        marker in str(row.get("content", "")) or marker in str(row.get("name", ""))
        for row in hits
    )


def _wait(predicate, ctx, message, timeout=None):
    deadline = time.monotonic() + (timeout or ctx.config.build_timeout)
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(min(0.5, ctx.config.poll_interval))
    raise AssertionError(f"{message}; last={last!r}")


def _wait_upload_build(ctx, project):
    def find():
        rows = [row for row in _builds(ctx, project) if row["triggered_by"] == "upload"]
        return rows[-1] if rows else None

    row = _wait(
        find,
        ctx,
        f"项目 {project} 没有出现上传触发的构建",
        timeout=min(ctx.config.build_timeout, 120),
    )

    def finished_build():
        current = _build(ctx, project, row["id"])
        return (
            current
            if current["status"] in {"succeeded", "failed", "cancelled"}
            else None
        )

    finished = _wait(
        finished_build,
        ctx,
        f"上传自动构建 {row['id']} 没有结束",
    )
    ctx.require(
        finished["status"] == "succeeded",
        f"上传自动构建 {row['id']} 失败: {finished.get('error')}",
    )
    return finished


def check_issue_224_default_debounce_starts_pending(ctx):
    settings = _settings(ctx, {})
    ctx.require(
        settings.get("auto_build_debounce_seconds") is None,
        f"本检查要求未设置去抖窗口，实际为 {settings.get('auto_build_debounce_seconds')!r}",
    )

    project = _create_project(ctx)
    project_id = project["id"]
    ctx.require(_builds(ctx, project_id) == [], "新项目不应已有构建")

    first_marker = f"issue224first{ctx.generation.replace('_', '')}"
    _upload(ctx, project_id, first_marker)

    pending = _wait(
        lambda: (
            _status(ctx, project_id)
            if _status(ctx, project_id)["state"] == "pending"
            else None
        ),
        ctx,
        "未设置去抖窗口时上传没有进入待构建状态",
        timeout=10,
    )
    due = pending["due_in_seconds"]
    ctx.logger.line(f"ISSUE224_PENDING {pending}")
    ctx.require(
        due is not None and 25 <= due <= DEFAULT_WINDOW + 1,
        f"缺失设置时倒计时应约为 {DEFAULT_WINDOW} 秒，实际 {due!r}",
    )
    ctx.require(due <= DEFAULT_WINDOW, f"倒计时超过默认窗口: {due}")

    time.sleep(1.2)
    ctx.require(
        _builds(ctx, project_id) == [],
        "未设置去抖窗口时上传后立即产生了构建",
    )

    _settings(ctx, {"auto_build_debounce_seconds": str(SHORT_WINDOW)})
    second_marker = f"issue224second{ctx.generation.replace('_', '')}"
    _upload(ctx, project_id, second_marker)

    build = _wait_upload_build(ctx, project_id)
    ctx.logger.line(
        f"ISSUE224_BUILD build={build['id']} type={build['build_type']} "
        f"status={build['status']}"
    )
    ctx.require(build["build_type"] == "full", build)
    ctx.require(
        len(
            [row for row in _builds(ctx, project_id) if row["triggered_by"] == "upload"]
        )
        == 1,
        "两次上传没有合并成一个构建",
    )

    effective = _wait(
        lambda: (
            _status(ctx, project_id)
            if _status(ctx, project_id)["state"] == "effective"
            else None
        ),
        ctx,
        "去抖窗口过后的自动构建没有让源状态变成 effective",
        timeout=120,
    )
    ctx.logger.line(f"ISSUE224_STATUS {effective}")

    for marker in (first_marker, second_marker):
        hits = _wait(
            lambda marker=marker: (
                _search(ctx, project_id, marker)
                if _contains(_search(ctx, project_id, marker), marker)
                else None
            ),
            ctx,
            f"去抖窗口过后的构建检索不到 {marker}",
            timeout=120,
        )
        ctx.logger.line(f"ISSUE224_SEARCH marker={marker} hits={len(hits)}")
