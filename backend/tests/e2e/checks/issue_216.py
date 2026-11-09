"""首次上传自动全量构建并在源文件页生效 (#216).

The harness has already built its default project.  This check creates a new
document project, uploads a Markdown file without requesting a build, and waits
for the upload-triggered automatic build to finish.  The first build must be
``full`` (an incremental build would fail before it starts), the source status
must become ``effective``, and the uploaded marker must be searchable.  A
second upload then proves the follow-up build is incremental once a successful
full build exists.
"""

from __future__ import annotations

import base64
import time


def _payload(text):
    return base64.b64encode(text.encode()).decode()


def _request(ctx, method, path, *, project, **kwargs):
    params = dict(kwargs.pop("params", {}))
    params["project"] = project
    return ctx.rest.request(method, path, params=params, **kwargs)


def _settings(ctx, values):
    response = ctx.rest.put("/api/v1/settings", json={"values": values})
    ctx.require(response.status_code == 200, response.text[:300])


def _create_project(ctx):
    response = ctx.rest.post(
        "/api/v1/projects",
        json={"name": f"issue216-{ctx.generation[-6:]}", "type": "document"},
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
            "category": "issue216",
            "files": [
                {
                    "path": f"{marker}.md",
                    "content_base64": _payload(
                        f"# {marker}\n\n{marker} 是首次上传自动构建的检索标记。\n"
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


def _wait_upload_build(ctx, project, *, build_type):
    def find():
        rows = [
            row
            for row in _builds(ctx, project)
            if row["triggered_by"] == "upload" and row["build_type"] == build_type
        ]
        return rows[-1] if rows else None

    row = _wait(
        find,
        ctx,
        f"项目 {project} 没有出现 triggered_by=upload 的 {build_type} 构建",
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


def check_issue_216_first_upload_builds_full(ctx):
    _settings(
        ctx,
        {
            "auto_build_debounce_seconds": "1",
            "embedding_url": ctx.config.embedding_url,
            "embedding_model": ctx.config.embedding_model,
        },
    )
    project = _create_project(ctx)
    project_id = project["id"]
    ctx.require(_builds(ctx, project_id) == [], "新项目不应已有构建")

    first_marker = f"issue216first{ctx.generation.replace('_', '')}"
    _upload(ctx, project_id, first_marker)
    first = _wait_upload_build(ctx, project_id, build_type="full")
    ctx.require(first["triggered_by"] == "upload", first)
    ctx.logger.line(
        f"ISSUE216_FIRST build={first['id']} type={first['build_type']} "
        f"status={first['status']}"
    )

    effective = _wait(
        lambda: (
            _status(ctx, project_id)
            if _status(ctx, project_id)["state"] == "effective"
            else None
        ),
        ctx,
        "首次上传自动构建结束后源状态没有变成 effective",
        timeout=120,
    )
    ctx.logger.line(f"ISSUE216_STATUS {effective}")

    hits = _wait(
        lambda: (
            _search(ctx, project_id, first_marker)
            if _contains(_search(ctx, project_id, first_marker), first_marker)
            else None
        ),
        ctx,
        "首次上传的内容在自动构建后检索不到",
        timeout=120,
    )
    ctx.logger.line(f"ISSUE216_SEARCH hits={len(hits)}")

    second_marker = f"issue216second{ctx.generation.replace('_', '')}"
    _upload(ctx, project_id, second_marker)
    second = _wait_upload_build(ctx, project_id, build_type="incremental")
    ctx.require(second["triggered_by"] == "upload", second)
    ctx.logger.line(
        f"ISSUE216_SECOND build={second['id']} type={second['build_type']} "
        f"status={second['status']}"
    )
    _wait(
        lambda: (
            _search(ctx, project_id, second_marker)
            if _contains(_search(ctx, project_id, second_marker), second_marker)
            else None
        ),
        ctx,
        "已有成功全量后的增量上传检索不到新内容",
        timeout=120,
    )
