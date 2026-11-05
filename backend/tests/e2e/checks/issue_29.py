"""Issue #29: every REST time field carries an explicit UTC offset."""

import re

TIME_NAME = re.compile(r"(^|_)(at|date|time|timestamp)$")
UTC_SUFFIX = re.compile(r"Z$")


def _utc_failures(payload, path=""):
    failures = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}" if path else key
            if (
                TIME_NAME.search(key)
                and value is not None
                and (not isinstance(value, str) or not UTC_SUFFIX.search(value))
            ):
                failures.append(f"{child}={value!r}")
            failures.extend(_utc_failures(value, child))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            failures.extend(_utc_failures(value, f"{path}[{index}]"))
    return failures


def check_rest_time_fields_are_utc(ctx):
    checked = []
    failures = []
    issued = ctx.rest.post(
        "/api/v1/tokens", json={"name": "issue-29 token", "role": "user"}
    )
    ctx.require(issued.status_code == 201, f"token issue: {issued.text}")
    checked.append("/api/v1/tokens (POST)")
    failures.extend(_utc_failures(issued.json(), "/api/v1/tokens"))
    revoked = ctx.rest.delete(f"/api/v1/tokens/{issued.json()['id']}")
    ctx.require(revoked.status_code == 204, f"token revoke: {revoked.text}")
    for path in ("/api/v1/tokens", "/api/v1/builds", "/api/v1/generations"):
        response = ctx.rest.get(path)
        ctx.require(response.status_code == 200, f"{path}: {response.status_code}")
        checked.append(path)
        failures.extend(_utc_failures(response.json(), path))
    detail = ctx.rest.get(f"/api/v1/builds/{ctx.build_id}")
    ctx.require(detail.status_code == 200, f"build detail: {detail.text}")
    checked.append(f"/api/v1/builds/{ctx.build_id}")
    failures.extend(_utc_failures(detail.json(), "/api/v1/builds"))

    provider = ctx.rest.post(
        "/api/v1/providers",
        json={
            "name": "issue-29 provider",
            "type": "openai-completions",
            "base_url": "http://127.0.0.1:1",
            "models": ["issue-29-model"],
        },
    )
    ctx.require(provider.status_code == 201, f"provider create: {provider.text}")
    checked.append("/api/v1/providers")
    failures.extend(_utc_failures(provider.json(), "/api/v1/providers"))
    deleted = ctx.rest.delete(f"/api/v1/providers/{provider.json()['id']}")
    ctx.require(deleted.status_code == 204, f"provider delete: {deleted.text}")

    conversations = ctx.rest.get("/api/v1/conversations")
    ctx.require(
        conversations.status_code == 200, f"conversations: {conversations.text}"
    )
    checked.append("/api/v1/conversations")
    failures.extend(_utc_failures(conversations.json(), "/api/v1/conversations"))

    ctx.require(not failures, "; ".join(failures))
    return {"endpoints": checked}
