"""End-to-end project isolation for MCP tools and agent credentials (#178).

The harness has already built project A (``default``).  This check adds a
distinguishable Markdown document to A, creates document project B with its
own Markdown document, builds both, then enters through the real MCP HTTP
endpoint as four actors:

- ``ua``: member of A only
- ``ub``: member of B only
- ``uab``: member of both A and B (multi-project token)
- system administrator (the harness token)

Every read tool is called once for each project and once with the other
project selected; the latter must fail with the same "项目不存在" error as a
missing project.  It also checks agent credentials locked to A, correction and
knowledge-gap submission into B, capabilities, and the explicit
``ask_dataagent`` limitation.
"""

from __future__ import annotations

import base64
import os
import time
import uuid

BATCH = 2000
NOT_FOUND = "项目不存在"


def _upload(ctx, project_id, category, marker, filename="probe.md"):
    response = ctx.rest.post(
        "/api/v1/source/upload",
        params={"project": project_id},
        json={
            "target": f"knowledge/{category}",
            "files": [
                {
                    "path": filename,
                    "content_base64": base64.b64encode(
                        f"# {category}\n\n{marker} belongs to {project_id}.\n".encode()
                    ).decode(),
                }
            ],
        },
    )
    ctx.require(
        response.status_code == 200, f"上传 {project_id} 失败: {response.text[:300]}"
    )
    return response.json()["commit"]


def _wait_build(ctx, project_id, build_id, timeout=None):
    deadline = time.monotonic() + (timeout or ctx.config.build_timeout)
    while time.monotonic() < deadline:
        response = ctx.rest.get(
            f"/api/v1/builds/{build_id}", params={"project": project_id}
        )
        ctx.require(
            response.status_code == 200,
            f"构建 {build_id} 详情 HTTP {response.status_code}: {response.text[:200]}",
        )
        detail = response.json()
        if detail.get("status") in {"succeeded", "failed", "cancelled"}:
            return detail
        time.sleep(ctx.config.poll_interval)
    raise AssertionError(f"项目 {project_id} 的构建 {build_id} 超时")


def _build(ctx, project_id, build_type="incremental"):
    response = ctx.rest.post(
        "/api/v1/builds",
        params={"project": project_id},
        json={"build_type": build_type},
    )
    ctx.require(response.status_code == 202, response.text[:300])
    build = _wait_build(ctx, project_id, response.json()["id"])
    ctx.require(
        build["status"] == "succeeded",
        f"项目 {project_id} 构建失败: {build.get('status')} {build.get('error')}",
    )
    return build


def _value(result):
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if "result" in structured:
            return structured["result"]
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            import json

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def _outcome(session, name, arguments=None):
    result = session.call_tool(name, arguments or {})
    if getattr(result, "isError", False):
        body = getattr(result, "structuredContent", None) or {}
        return {
            "error": body.get("code"),
            "message": body.get("message", ""),
        }
    return {"ok": _value(result)}


def _require_ok(ctx, outcome, label, predicate=None):
    ctx.require(
        "error" not in outcome,
        f"{label} 不应失败: {outcome.get('error')}: {outcome.get('message')}",
    )
    value = outcome["ok"]
    if predicate is not None:
        ctx.require(predicate(value), f"{label} 返回了其它项目的内容: {value!r}")
    return value


def _require_not_found(ctx, outcome, label):
    ctx.require(
        outcome.get("error") == "invalid_input" and outcome.get("message") == NOT_FOUND,
        f"{label} 越权访问应返回“{NOT_FOUND}”，实际 {outcome}",
    )


def _issue_token(ctx, name, projects, issued):
    response = ctx.rest.post(
        "/api/v1/tokens",
        json={"name": name, "role": "user", "projects": projects},
    )
    ctx.require(response.status_code == 201, response.text[:300])
    token = response.json()
    issued.append(token)
    return token


def _configure_agent_credential(ctx, owner_id, project_id):
    """Create a running turn and issue a real agent credential for it."""
    from semantica_dataagent.auth import issue_agent_credential
    from semantica_dataagent.db import Conversation, Turn, sessions

    os.environ["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    conversation_id = "issue178-" + uuid.uuid4().hex
    with sessions().begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_id=owner_id,
                title="issue 178 agent",
                agent="test",
                provider_id="test",
                model="test",
            )
        )
        db.flush()
        turn = Turn(
            conversation_id=conversation_id,
            provider_id="test",
            model="test",
            sequence=1,
            question="issue 178",
            status="running",
            answer="",
            sql="[]",
            citations="[]",
        )
        db.add(turn)
        db.flush()
        turn_id = turn.id
    credential = issue_agent_credential(
        owner_id, conversation_id, turn_id, project_id=project_id
    )
    return credential, {
        "x-dataagent-conversation-id": conversation_id,
        "x-dataagent-turn-id": str(turn_id),
    }


def _cleanup(ctx, project_id):
    import sqlite3

    with sqlite3.connect(ctx.runtime.db_path) as db:
        builds = db.execute(
            "SELECT generation FROM builds WHERE project_id = ? AND generation IS NOT NULL",
            (project_id,),
        ).fetchall()
        pointer = db.execute(
            "SELECT generation FROM generation_pointer WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    generations = sorted({row[0] for row in builds} | {row[0] for row in pointer})
    result = {"generations": generations}
    try:
        from neo4j import GraphDatabase

        with GraphDatabase.driver(
            ctx.runtime.config.neo4j_uri,
            auth=(ctx.runtime.config.neo4j_user, ctx.runtime.password),
            connection_timeout=10,
        ) as driver:
            query = "MATCH (n:SDAEntity) WHERE n.generation IN $generations"
            for deletion in (
                " MATCH (n)-[r]-() WITH DISTINCT r LIMIT $batch DELETE r RETURN count(*) AS n",
                " WITH n LIMIT $batch DETACH DELETE n RETURN count(*) AS n",
            ):
                while True:
                    deleted, _, _ = driver.execute_query(
                        query + deletion, generations=generations, batch=BATCH
                    )
                    if not deleted[0]["n"]:
                        break
            remaining, _, _ = driver.execute_query(
                query + " RETURN count(n) AS n", generations=generations
            )
            result["neo4j_remaining"] = remaining[0]["n"]
    except Exception as exc:  # noqa: BLE001 - reported through the check result
        result["neo4j_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=ctx.runtime.config.milvus_uri, timeout=10)
        try:
            names = set(client.list_collections())
            for generation in generations:
                collection = "sda_" + generation
                if collection in names:
                    client.drop_collection(collection)
            remaining = {"sda_" + generation for generation in generations} & set(
                client.list_collections()
            )
            result["milvus_remaining"] = sorted(remaining)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        result["milvus_error"] = f"{type(exc).__name__}: {exc}"
    return result


def check_issue_178_mcp_project_scope(ctx):
    suffix = uuid.uuid4().hex[:8]
    marker_a = f"issue178markerA{suffix}"
    marker_b = f"issue178markerB{suffix}"
    category_a = f"issue178a{suffix}"
    category_b = f"issue178b{suffix}"
    project_b = None
    issued = []
    sessions_opened = []
    table = []
    try:
        _upload(ctx, "default", category_a, marker_a)
        build_a = _build(ctx, "default", "incremental")
        ref_a = f"document:{category_a}/probe.md"

        created = ctx.rest.post(
            "/api/v1/projects",
            json={"name": f"Issue 178 Project B {suffix}", "type": "document"},
        )
        ctx.require(created.status_code == 201, created.text[:300])
        project_b = created.json()
        project_b_id = project_b["id"]
        _upload(ctx, project_b_id, category_b, marker_b)
        build_b = _build(ctx, project_b_id, "full")
        ref_b = f"document:{category_b}/probe.md"

        ua = _issue_token(
            ctx,
            f"issue178-ua-{suffix}",
            [{"project_id": "default", "role": "member"}],
            issued,
        )
        ub = _issue_token(
            ctx,
            f"issue178-ub-{suffix}",
            [{"project_id": project_b_id, "role": "member"}],
            issued,
        )
        uab = _issue_token(
            ctx,
            f"issue178-uab-{suffix}",
            [
                {"project_id": "default", "role": "member"},
                {"project_id": project_b_id, "role": "member"},
            ],
            issued,
        )

        def open_mcp(token, *, project=None, headers=None):
            session = ctx.open_mcp(token, project=project, headers=headers)
            sessions_opened.append(session)
            return session

        admin = open_mcp(ctx.token)
        a_only = open_mcp(ua["token"])
        b_only = open_mcp(ub["token"])
        ab_none = open_mcp(uab["token"])
        ab_a = open_mcp(uab["token"], project="default")
        ab_b = open_mcp(uab["token"], project=project_b_id)
        ab_b_header = open_mcp(
            uab["token"], headers={"X-Dataagent-Project": project_b_id}
        )

        # A multi-project token must choose; the error lists id and name.
        no_choice = _outcome(ab_none, "get_capabilities")
        ctx.require(no_choice.get("error") == "invalid_input", no_choice)
        message = no_choice.get("message", "")
        ctx.require(message.startswith("请指定项目"), message)
        default_name = next(
            row["name"]
            for row in ctx.rest.get("/api/v1/projects").json()
            if row["id"] == "default"
        )
        ctx.require(f"default（{default_name}）" in message, message)
        ctx.require(f"{project_b_id}（{project_b['name']}）" in message, message)

        # Single-project tokens keep their historical default behavior.
        ua_default = _outcome(a_only, "list_categories")
        _require_ok(
            ctx,
            ua_default,
            "ua 不指定项目",
            lambda body: any(row.get("name") == category_a for row in body),
        )
        ub_default = _outcome(b_only, "list_categories")
        _require_ok(
            ctx,
            ub_default,
            "ub 不指定项目",
            lambda body: any(row.get("name") == category_b for row in body),
        )
        ab_a_value = _outcome(ab_a, "list_categories")
        _require_ok(
            ctx,
            ab_a_value,
            "uab 查询参数指定 A",
            lambda body: any(row.get("name") == category_a for row in body),
        )
        ab_b_value = _outcome(ab_b, "list_categories")
        _require_ok(
            ctx,
            ab_b_value,
            "uab 查询参数指定 B",
            lambda body: any(row.get("name") == category_b for row in body),
        )
        ab_b_header_value = _outcome(ab_b_header, "list_categories")
        _require_ok(
            ctx,
            ab_b_header_value,
            "uab 请求头指定 B",
            lambda body: any(row.get("name") == category_b for row in body),
        )
        ctx.require(
            not any(row.get("name") == category_b for row in ab_a_value["ok"]),
            "uab 指定 A 时读到了 B 的类别",
        )
        ctx.require(
            not any(row.get("name") == category_a for row in ab_b_value["ok"]),
            "uab 指定 B 时读到了 A 的类别",
        )

        # get_capabilities carries the resolved project and accessible list.
        def project_id(body, expected):
            return (body.get("project") or {}).get("id") == expected

        caps_admin = _require_ok(ctx, _outcome(admin, "get_capabilities"), "管理员能力")
        ctx.require(project_id(caps_admin, "default"), caps_admin)
        ids = {row["id"] for row in caps_admin.get("projects", [])}
        ctx.require({"default", project_b_id} <= ids, caps_admin.get("projects"))
        caps_a = _require_ok(
            ctx,
            _outcome(ab_a, "get_capabilities"),
            "uab A 能力",
            lambda b: project_id(b, "default"),
        )
        ctx.require(
            {row["id"] for row in caps_a["projects"]} == {"default", project_b_id},
            caps_a["projects"],
        )
        caps_b = _require_ok(
            ctx,
            _outcome(ab_b, "get_capabilities"),
            "uab B 能力",
            lambda b: project_id(b, project_b_id),
        )
        ctx.require(
            {row["id"] for row in caps_b["projects"]} == {"default", project_b_id},
            caps_b["projects"],
        )

        def a_search(body):
            return any(str(row.get("ref", "")).startswith(ref_a) for row in body)

        def b_search(body):
            return any(str(row.get("ref", "")).startswith(ref_b) for row in body)

        def b_empty(body):
            return body == []

        def a_no_path(body):
            return body.get("status") == "no_path" and "No connection" in body.get(
                "reason", ""
            )

        def b_no_path(body):
            return body.get("status") == "no_path" and "Table missing" in body.get(
                "reason", ""
            )

        def a_lineage(body):
            return bool(body) and all(
                row.get("upstream") and row.get("task_id") for row in body
            )

        def a_validate(body):
            return body.get("status") == "pass"

        def b_not_pass(body):
            return body.get("status") != "pass"

        def a_task(body):
            return body.get("name") == "load_orders" and "shop.orders" in body.get(
                "sql", ""
            )

        def a_doc(body):
            return marker_a in body.get("content", "") and body.get("ref") == ref_a

        def b_doc(body):
            return marker_b in body.get("content", "") and body.get("ref") == ref_b

        rows = [
            {
                "tool": "describe_tables",
                "a": {"names": ["shop.orders"]},
                "b": {"names": ["shop.orders"]},
                "check_a": lambda body: any(
                    row.get("name") == "orders" for row in body
                ),
                "check_b": b_empty,
            },
            {
                "tool": "search_knowledge",
                "a": {"query": marker_a, "scope": "docs", "top_k": 10},
                "b": {"query": marker_b, "scope": "docs", "top_k": 10},
                "check_a": a_search,
                "check_b": b_search,
            },
            {
                "tool": "lookup_dim_values",
                "a": {"table": "shop.channels", "keyword": "网页"},
                "b": {"table": "shop.channels", "keyword": "网页"},
                "check_a": lambda body: any(row.get("value") == "网页" for row in body),
                "check_b": b_empty,
            },
            {
                "tool": "list_categories",
                "a": {},
                "b": {},
                "check_a": lambda body: any(
                    row.get("name") == category_a for row in body
                ),
                "check_b": lambda body: any(
                    row.get("name") == category_b for row in body
                ),
            },
            {
                "tool": "read_category_index",
                "a": {"category": "metrics"},
                "b": {"category": category_b},
                "check_a": lambda body: str(body.get("ref", "")).startswith(
                    "document:metrics/"
                ),
                "check_b": lambda body: str(body.get("ref", "")).startswith(
                    f"document:{category_b}/"
                ),
            },
            {
                "tool": "read_document",
                "a": {"doc_id": "document:metrics/sales.md"},
                "b": {"doc_id": ref_b},
                "check_a": lambda body: "sales" in body.get("content", "").lower(),
                "check_b": b_doc,
            },
            {
                "tool": "get_lineage",
                "a": {"table": "shop.orders", "direction": "up", "depth": 1},
                "b": {"table": "shop.orders", "direction": "up", "depth": 1},
                "check_a": a_lineage,
                "check_b": b_empty,
            },
            {
                "tool": "read_task_sql",
                "a": {"task_id": "load_orders"},
                "b": {"task_id": "load_orders"},
                "check_a": a_task,
                "check_b": lambda _body: True,
                "expect_error_b": "invalid_input",
            },
            {
                "tool": "validate_sql",
                "a": {
                    "sql": "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)",
                    "dialect": "hive",
                },
                "b": {
                    "sql": "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)",
                    "dialect": "hive",
                },
                "check_a": a_validate,
                "check_b": b_not_pass,
            },
            {
                "tool": "find_join_path",
                "a": {"tables": ["shop.orders", "shop.order_items"]},
                "b": {"tables": ["shop.orders", "shop.order_items"]},
                "check_a": a_no_path,
                "check_b": b_no_path,
            },
            {
                "tool": "request_clarification",
                "a": {
                    "questions": [
                        {
                            "question": "Which definition?",
                            "options": [
                                {"label": "sales", "ref": "document:metrics/sales.md"},
                                {
                                    "label": "orders",
                                    "ref": "document:metrics/orders.md",
                                },
                            ],
                        }
                    ]
                },
                "b": {
                    "questions": [
                        {
                            "question": "Which definition?",
                            "options": [
                                {"label": "probe", "ref": ref_b},
                                {"label": "probe again", "ref": ref_b},
                            ],
                        }
                    ]
                },
                "check_a": lambda _body: True,
                "check_b": lambda _body: True,
            },
            {
                "tool": "sample_table_data",
                "a": {"table": "shop.orders", "limit": 2},
                "b": {"table": "shop.orders", "limit": 2},
                "special": True,
            },
            {
                "tool": "propose_query",
                "a": {
                    "sql": "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)"
                },
                "b": {
                    "sql": "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)"
                },
                "special": True,
            },
            {
                "tool": "execute_query",
                "a": {
                    "sql": "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)"
                },
                "b": {
                    "sql": "SELECT id FROM shop.orders WHERE dt = (SELECT MAX(dt) FROM shop.orders)"
                },
                "special": True,
            },
            {
                "tool": "read_document",
                "a": {"doc_id": ref_a},
                "b": {"doc_id": ref_b},
                "check_a": a_doc,
                "check_b": b_doc,
            },
        ]

        # Cross-project sessions: every tool must reject the other project.
        cross_a_from_ub = open_mcp(ub["token"], project="default")
        cross_b_from_ua = open_mcp(ua["token"], project=project_b_id)

        for row in rows:
            name = row["tool"]
            a_args = row["a"]
            b_args = row["b"]
            outcome_a = _outcome(a_only, name, a_args)
            outcome_b = _outcome(b_only, name, b_args)
            if row.get("special"):
                if name == "sample_table_data":
                    ctx.require(
                        outcome_a.get("error") == "unavailable" or "ok" in outcome_a,
                        f"A 的取样应到达本项目运行时或明确不可用: {outcome_a}",
                    )
                    ctx.require(
                        outcome_b.get("error") == "invalid_input"
                        and "不在当前知识代" in outcome_b.get("message", ""),
                        f"B 的取样应说明表不在 B: {outcome_b}",
                    )
                elif name in {"propose_query", "execute_query"}:
                    ctx.require(
                        outcome_a.get("error")
                        in {"unavailable", "internal_error", "invalid_input"},
                        f"A 的 {name} 应到达本项目校验: {outcome_a}",
                    )
                    a_message = outcome_a.get("message", "")
                    ctx.require(
                        not (
                            outcome_a.get("error") == "invalid_input"
                            and (
                                "SQL validation did not pass" in a_message
                                or "无法确定表的分区元数据" in a_message
                            )
                        ),
                        f"A 的 {name} 被其它项目元数据拒绝: {outcome_a}",
                    )
                    b_message = outcome_b.get("message", "")
                    ctx.require(
                        outcome_b.get("error") == "invalid_input"
                        and (
                            "SQL validation did not pass" in b_message
                            or "无法确定表的分区元数据" in b_message
                        ),
                        f"B 的 {name} 应使用 B 的空元数据: {outcome_b}",
                    )
                status_a = outcome_a.get("error") or "ok"
                status_b = outcome_b.get("error") or "ok"
            else:
                if row.get("expect_error_b"):
                    ctx.require(
                        outcome_b.get("error") == row["expect_error_b"]
                        and outcome_b.get("message") != NOT_FOUND,
                        f"B 的 {name} 应使用 B 自己的空内容: {outcome_b}",
                    )
                    status_b = "invalid_input(own)"
                else:
                    _require_ok(ctx, outcome_b, f"ub 读取 B 的 {name}", row["check_b"])
                    status_b = "ok(own)"
                _require_ok(ctx, outcome_a, f"ua 读取 A 的 {name}", row["check_a"])
                status_a = "ok(own)"

            denied_a = _outcome(cross_a_from_ub, name, a_args)
            denied_b = _outcome(cross_b_from_ua, name, b_args)
            _require_not_found(ctx, denied_a, f"ub 读取 A 的 {name}")
            _require_not_found(ctx, denied_b, f"ua 读取 B 的 {name}")
            table.append(
                {
                    "tool": name,
                    "A": status_a,
                    "B": status_b,
                    "ub->A": NOT_FOUND,
                    "ua->B": NOT_FOUND,
                }
            )

        # A nonexistent project is indistinguishable from a forbidden one.
        missing = open_mcp(uab["token"], project="missing" + suffix)
        _require_not_found(
            ctx, _outcome(missing, "list_categories"), "请求不存在的项目"
        )

        # Agent credential bound to A cannot be overridden by query or header.
        credential, credential_headers = _configure_agent_credential(
            ctx, uab["id"], "default"
        )
        agent_a = open_mcp(credential, headers=credential_headers)
        agent_override_query = open_mcp(
            credential,
            project=project_b_id,
            headers=credential_headers,
        )
        agent_override_header = open_mcp(
            credential,
            headers={**credential_headers, "X-Dataagent-Project": project_b_id},
        )
        _require_ok(
            ctx,
            _outcome(agent_a, "list_categories"),
            "agent 凭据读取 A",
            lambda body: any(row.get("name") == category_a for row in body),
        )
        _require_ok(
            ctx,
            _outcome(agent_a, "search_knowledge", {"query": marker_a, "scope": "docs"}),
            "agent 凭据检索 A",
            a_search,
        )
        _require_not_found(
            ctx, _outcome(agent_override_query, "list_categories"), "agent 查询参数覆盖"
        )
        _require_not_found(
            ctx, _outcome(agent_override_header, "list_categories"), "agent 请求头覆盖"
        )

        # Correction and knowledge gap are written into the selected project.
        correction = _require_ok(
            ctx,
            _outcome(
                ab_b,
                "propose_correction",
                {
                    "target": ref_b,
                    "patch": f"# {category_b}\n\ncorrected {marker_b}\n",
                    "reason": "issue 178 e2e",
                },
            ),
            "B 纠错提交",
        )
        proposal_id = correction.get("proposal_id")
        ctx.require(proposal_id, correction)
        proposals_b = ctx.rest.get(
            "/api/v1/corrections", params={"project": project_b_id}
        )
        ctx.require(proposals_b.status_code == 200, proposals_b.text[:300])
        ctx.require(
            any(row["id"] == proposal_id for row in proposals_b.json()),
            "B 纠错没有落在 B",
        )
        proposals_a = ctx.rest.get("/api/v1/corrections", params={"project": "default"})
        ctx.require(proposals_a.status_code == 200, proposals_a.text[:300])
        ctx.require(
            all(row["id"] != proposal_id for row in proposals_a.json()),
            "B 纠错错误地落在 A",
        )

        gap = _require_ok(
            ctx,
            _outcome(
                ab_b,
                "report_knowledge_gap",
                {"question": f"gap {marker_b}", "missing": "missing B knowledge"},
            ),
            "B 知识缺口提交",
        )
        gap_id = gap.get("gap_id")
        ctx.require(gap_id, gap)
        gaps_b = ctx.rest.get(
            "/api/v1/knowledge-gaps", params={"project": project_b_id}
        )
        ctx.require(gaps_b.status_code == 200, gaps_b.text[:300])
        ctx.require(any(row["id"] == gap_id for row in gaps_b.json()), "缺口没有落在 B")
        gaps_a = ctx.rest.get("/api/v1/knowledge-gaps", params={"project": "default"})
        ctx.require(gaps_a.status_code == 200, gaps_a.text[:300])
        ctx.require(
            all(row["id"] != gap_id for row in gaps_a.json()), "缺口错误地落在 A"
        )

        # ask_dataagent remains default-project only in this ticket.
        ask = _outcome(ab_b, "ask_dataagent", {"question": "what is sales?"})
        ctx.require(
            ask.get("error") == "invalid_input"
            and "项目问答将在对话绑定项目后支持" in ask.get("message", ""),
            f"非默认项目的 ask_dataagent 应明确拒绝: {ask}",
        )

        return {
            "project_a": "default",
            "project_b": project_b_id,
            "actors": {"ua": ua["id"], "ub": ub["id"], "uab": uab["id"]},
            "builds": {"a": build_a["id"], "b": build_b["id"]},
            "mcp_table": table,
            "agent_credential": {
                "reads_a": True,
                "query_override": NOT_FOUND,
                "header_override": NOT_FOUND,
            },
            "writes": {"correction": proposal_id, "gap": gap_id},
        }
    finally:
        for session in sessions_opened:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                ctx.logger.line(f"issue178 mcp cleanup failed: {exc}")
        for token in issued:
            try:
                ctx.rest.delete(f"/api/v1/tokens/{token['id']}")
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                ctx.logger.line(f"issue178 token cleanup failed: {exc}")
        if project_b is not None:
            cleanup = _cleanup(ctx, project_b["id"])
            ctx.logger.line(f"issue178 cleanup: {cleanup}")
            ctx.runtime.storage_touched = True
