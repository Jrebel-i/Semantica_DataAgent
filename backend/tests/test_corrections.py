"""Acceptance checks for correction proposals and knowledge gaps."""

from test_clarification import _requester, call

pytest_plugins = ["test_clarification"]


def _project_member(requester, project_id="default", role="member"):
    from semantica_dataagent.db import ProjectMember, sessions

    with sessions().begin() as db:
        db.add(ProjectMember(project_id=project_id, token_id=requester.id, role=role))
    return requester


def test_knowledge_gap_is_recorded_for_the_requester(client):
    from semantica_dataagent.app import create_mcp

    c, token = client
    requester = _requester(token="gap-reporter")
    result = call(
        create_mcp(),
        "report_knowledge_gap",
        {"question": "销售额如何计算？", "missing": "支付口径定义"},
        requester,
    )
    assert not result.isError, result.structuredContent
    response = c.get(
        "/api/v1/knowledge-gaps", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    assert any(
        item["question"] == "销售额如何计算？" and item["missing"] == "支付口径定义"
        for item in response.json()
    )


def test_user_cannot_approve_correction(client):
    c, _ = client
    requester = _project_member(_requester(role="user", token="correction-reader"))
    response = c.post(
        "/api/v1/corrections/nonexistent/approve",
        headers={"Authorization": f"Bearer {requester.token}"},
    )
    assert response.status_code == 403


def test_proposal_rejects_target_absent_from_current_generation(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set())
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/missing.md",
            "patch": "修正后的定义\n",
            "reason": "定义有误",
        },
        _requester(token="missing-target"),
    )
    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert result.structuredContent["message"] != "Unknown tool"


def test_correction_and_gap_tools_publish_self_contained_schemas():
    import json

    from semantica_dataagent.app import create_mcp

    tools = {
        tool.name: tool.parameters for tool in create_mcp()._tool_manager.list_tools()
    }
    for name in ("propose_correction", "report_knowledge_gap"):
        assert name in tools, f"{name} is unavailable"
        schema = json.dumps(tools[name])
        assert '"$ref"' not in schema
        assert '"$defs"' not in schema


def test_user_cannot_reject_correction(client):
    c, _ = client
    requester = _project_member(_requester(role="user", token="rejection-reader"))
    response = c.post(
        "/api/v1/corrections/nonexistent/reject",
        headers={"Authorization": f"Bearer {requester.token}"},
    )
    assert response.status_code == 403


def test_proposal_rejects_another_users_conversation(client, monkeypatch):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp
    from semantica_dataagent.db import Conversation, sessions

    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    with sessions().begin() as db:
        db.add(
            Conversation(
                id="private-conversation",
                owner_id="someone-else",
                title="销售额",
                agent="pi",
                provider_id="provider",
                model="model",
            )
        )
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/sales.md",
            "patch": "修正后的定义\n",
            "reason": "定义有误",
        },
        _requester(token="different-owner"),
        "private-conversation",
    )
    assert result.isError
    assert result.structuredContent["code"] == "invalid_input"
    assert result.structuredContent["message"] != "Unknown tool"


def test_rejected_document_proposal_does_not_change_source(
    client, monkeypatch, tmp_path
):
    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    document = root / "knowledge" / "metrics" / "sales.md"
    document.parent.mkdir(parents=True)
    document.write_text("旧的销售额定义\n")
    configured = c.put(
        "/api/v1/settings",
        headers=headers,
        json={"values": {"source_directory": str(root)}},
    )
    assert configured.status_code == 200, configured.text
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [
            {
                "node": {
                    "content": document.read_text(),
                    "source_file": "knowledge/metrics/sales.md",
                }
            }
        ],
    )
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/sales.md",
            "patch": "新的销售额定义\n",
            "reason": "口径已更新",
        },
        _requester(token="reject-proposer"),
    )
    assert not result.isError, result.structuredContent
    proposal_id = result.structuredContent["proposal_id"]
    details = c.get(f"/api/v1/corrections/{proposal_id}", headers=headers)
    assert details.status_code == 200, details.text
    assert "-旧的销售额定义" in details.json()["diff"]
    assert "+新的销售额定义" in details.json()["diff"]
    response = c.post(f"/api/v1/corrections/{proposal_id}/reject", headers=headers)
    assert response.status_code == 200, response.text
    assert document.read_text() == "旧的销售额定义\n"
    assert (
        c.get(f"/api/v1/corrections/{proposal_id}", headers=headers).json()["status"]
        == "rejected"
    )


def test_approval_commits_document_and_enqueues_incremental_build(
    client, monkeypatch, tmp_path
):
    import subprocess

    from test_source_api import upload

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"source_directory": str(root)}},
        ).status_code
        == 200
    )
    assert (
        upload(c, headers, "knowledge/metrics", {"sales.md": "旧定义\n"}).status_code
        == 200
    )
    before = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [
            {
                "node": {
                    "content": "旧定义\n",
                    "source_file": "knowledge/metrics/sales.md",
                }
            }
        ],
    )
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/sales.md",
            "patch": "新定义\n",
            "reason": "口径已更新",
        },
        _requester(token="approval-proposer"),
    )
    assert not result.isError, result.structuredContent
    proposal_id = result.structuredContent["proposal_id"]
    response = c.post(f"/api/v1/corrections/{proposal_id}/approve", headers=headers)
    assert response.status_code == 200, response.text
    after = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    assert before != after
    assert (root / "knowledge/metrics/sales.md").read_text() == "新定义\n"
    message = subprocess.check_output(
        ["git", "-C", str(root), "log", "-1", "--format=%B"], text=True
    )
    assert proposal_id in message
    builds = c.get("/api/v1/builds", headers=headers).json()
    assert any(build["build_type"] == "incremental" for build in builds)


def test_metadata_override_survives_reimport_of_same_source(client, tmp_path):
    import json

    from semantica_dataagent.canonical import CanonicalAdapter
    from semantica_dataagent.db import MetadataOverride, sessions
    from semantica_dataagent.table_stage import apply_overrides

    source = tmp_path / "warehouse"
    source.mkdir()
    (source / "fields.json").write_text(
        json.dumps(
            [
                {
                    "database": "shop",
                    "table": "orders",
                    "name": "amount",
                    "type": "decimal",
                    "comment": "导出注释",
                    "partition": False,
                }
            ]
        )
    )
    with sessions().begin() as db:
        db.add(
            MetadataOverride(target="field:shop.orders.amount", comment="人工修正注释")
        )
    for _ in range(2):
        imported = CanonicalAdapter().read(source, {}).records
        corrected = apply_overrides(imported)
        assert corrected[0].data["comment"] == "人工修正注释"
        assert imported[0].data["comment"] == "导出注释"


def test_failed_atomic_replace_preserves_original_document(tmp_path, monkeypatch):
    from semantica_dataagent import corrections

    path = tmp_path / "document.md"
    path.write_text("original\n")

    def fail_replace(*_args):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(corrections.os, "replace", fail_replace)
    import pytest

    with pytest.raises(OSError, match="simulated rename failure"):
        corrections._atomic_write(path, "changed\n")
    assert path.read_text() == "original\n"
    assert list(tmp_path.iterdir()) == [path]


import pytest


@pytest.mark.parametrize("crash_at", ["precheck", "write", "add", "commit", "finalize"])
def test_interrupted_approval_recovers_without_duplicate_commits(
    client, monkeypatch, tmp_path, crash_at
):
    import os
    import signal
    import subprocess

    from test_source_api import upload

    from semantica_dataagent import corrections, knowledge
    from semantica_dataagent.auth import Requester
    from semantica_dataagent.db import Build, CorrectionProposal, sessions

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"source_directory": str(root)}},
        ).status_code
        == 200
    )
    assert (
        upload(c, headers, "knowledge/metrics", {"sales.md": "before\n"}).status_code
        == 200
    )
    requester = _requester(token="recovery-admin-" + crash_at)
    monkeypatch.setattr(knowledge, "read_graph", lambda _: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [{"node": {"source_file": "knowledge/metrics/sales.md"}}],
    )
    from semantica_dataagent import project_context

    monkeypatch.setattr(project_context, "wake_build_queue", lambda: None)
    with sessions().begin() as db:
        db.add(
            CorrectionProposal(
                id="recovery-" + crash_at,
                owner_id=requester.id,
                target="document:metrics/sales.md",
                patch="after\n",
                original_patch="after\n",
                original="before\n",
                diff="diff",
                reason="reason",
                status="pending",
            )
        )
    proposal = "recovery-" + crash_at
    if crash_at == "precheck":
        path = root / "knowledge/metrics/sales.md"
        path.write_text("someone else's staged content\n")
        subprocess.run(
            ["git", "-C", str(root), "add", "knowledge/metrics/sales.md"], check=True
        )
        path.write_text("before\n")
    pid = os.fork()
    if pid == 0:
        if crash_at == "precheck":

            def die_before_lock(_directory):
                os.kill(os.getpid(), signal.SIGKILL)

            corrections.repository = die_before_lock
        real_write = corrections._atomic_write
        real_git = corrections.git
        real_finalize = corrections._finalize

        def write_then_die(*args):
            result = real_write(*args)
            if crash_at == "write":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        def git_then_die(root, *args, **kwargs):
            result = real_git(root, *args, **kwargs)
            if args and args[0] == crash_at:
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        def finalize_then_die(*args):
            real_finalize(*args)
            os.kill(os.getpid(), signal.SIGKILL)

        corrections._atomic_write = write_then_die
        corrections.git = git_then_die
        if crash_at == "finalize":
            corrections._finalize = finalize_then_die
        corrections.approve(
            proposal,
            type("Scope", (), {"project_id": "default"})(),
            Requester(id=requester.id, role="admin", token=requester.token),
        )
        os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    corrections.recover_applying()
    corrections.recover_applying()
    with sessions()() as db:
        row = db.get(CorrectionProposal, proposal)
        expected = "approved" if crash_at in {"commit", "finalize"} else "pending"
        assert row.status == expected
        assert db.query(Build).filter_by(build_type="incremental").count() == (
            1 if expected == "approved" else 0
        )
    commits = subprocess.check_output(
        ["git", "-C", str(root), "log", "--format=%s"], text=True
    )
    assert commits.count("Approve correction " + proposal) == (
        1 if expected == "approved" else 0
    )
    if crash_at == "precheck":
        staged = subprocess.check_output(
            ["git", "-C", str(root), "show", ":knowledge/metrics/sales.md"], text=True
        )
        assert staged == "someone else's staged content\n"
    assert (root / "knowledge/metrics/sales.md").read_text() == (
        "after\n" if expected == "approved" else "before\n"
    )


def test_modified_approval_keeps_original_and_final_patches(
    client, monkeypatch, tmp_path
):
    from test_source_api import upload

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"source_directory": str(root)}},
        ).status_code
        == 200
    )
    assert (
        upload(c, headers, "knowledge/metrics", {"sales.md": "before\n"}).status_code
        == 200
    )
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [
            {
                "node": {
                    "content": "before\n",
                    "source_file": "knowledge/metrics/sales.md",
                }
            }
        ],
    )
    result = call(
        create_mcp(),
        "propose_correction",
        {
            "target": "document:metrics/sales.md",
            "patch": "proposed\n",
            "reason": "reason",
        },
        _requester(token="edited-proposer"),
    )
    assert not result.isError, result.structuredContent
    proposal = result.structuredContent["proposal_id"]
    response = c.post(
        f"/api/v1/corrections/{proposal}/approve",
        headers=headers,
        json={"patch": "admin edit\n"},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["original_patch"] == "proposed\n"
    assert detail["patch"] == "admin edit\n"
    assert "+admin edit" in detail["diff"]
    assert (root / "knowledge/metrics/sales.md").read_text() == "admin edit\n"


def test_approval_rejects_existing_staged_changes(client, monkeypatch, tmp_path):
    import subprocess

    from test_source_api import upload

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"source_directory": str(root)}},
        ).status_code
        == 200
    )
    assert (
        upload(c, headers, "knowledge/metrics", {"sales.md": "before\n"}).status_code
        == 200
    )
    (root / "knowledge/metrics/sales.md").write_text("external staged\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "knowledge/metrics/sales.md"], check=True
    )
    (root / "knowledge/metrics/sales.md").write_text("before\n")
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [
            {
                "node": {
                    "content": "before\n",
                    "source_file": "knowledge/metrics/sales.md",
                }
            }
        ],
    )
    result = call(
        create_mcp(),
        "propose_correction",
        {"target": "document:metrics/sales.md", "patch": "after\n", "reason": "reason"},
        _requester(token="staged-proposer"),
    )
    proposal = result.structuredContent["proposal_id"]
    response = c.post(f"/api/v1/corrections/{proposal}/approve", headers=headers)
    assert response.status_code == 409 and "staged" in response.text
    assert (root / "knowledge/metrics/sales.md").read_text() == "before\n"
    assert (
        c.get(f"/api/v1/corrections/{proposal}", headers=headers).json()["status"]
        == "pending"
    )
    assert "knowledge/metrics/sales.md" in subprocess.check_output(
        ["git", "-C", str(root), "diff", "--cached", "--name-only"], text=True
    )


def test_concurrent_admin_approvals_only_one_succeeds(client, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from test_source_api import upload

    from semantica_dataagent import knowledge
    from semantica_dataagent.app import create_mcp

    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    root = tmp_path / "source"
    assert (
        c.put(
            "/api/v1/settings",
            headers=headers,
            json={"values": {"source_directory": str(root)}},
        ).status_code
        == 200
    )
    assert (
        upload(c, headers, "knowledge/metrics", {"sales.md": "before\n"}).status_code
        == 200
    )
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "read_graph", lambda values: object())
    monkeypatch.setattr(
        knowledge,
        "execute",
        lambda *args: [
            {
                "node": {
                    "content": "before\n",
                    "source_file": "knowledge/metrics/sales.md",
                }
            }
        ],
    )
    result = call(
        create_mcp(),
        "propose_correction",
        {"target": "document:metrics/sales.md", "patch": "after\n", "reason": "reason"},
        _requester(token="concurrent-proposer"),
    )
    proposal = result.structuredContent["proposal_id"]
    barrier = Barrier(2)

    def approve(_):
        barrier.wait()
        return c.post(
            f"/api/v1/corrections/{proposal}/approve", headers=headers
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(approve, range(2)))
    assert sorted(statuses) == [200, 409]


def test_full_build_applies_field_override_on_reimport(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from test_build_queue import configure

    from semantica_dataagent import knowledge, relationships, stages
    from semantica_dataagent.canonical import Record
    from semantica_dataagent.db import Build, MetadataOverride, sessions
    from semantica_dataagent.stage_contract import StageResult, StorageHandles

    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        knowledge,
        "settings",
        lambda: {
            "source_directory": str(tmp_path),
            "knowledge_generation": "old",
            "embedding_model": "test",
            "embedding_url": "http://localhost",
            "neo4j_uri": "bolt://localhost",
            "neo4j_user": "neo4j",
            "milvus_uri": "http://localhost",
        },
    )
    monkeypatch.setattr(knowledge, "configured", list)
    monkeypatch.setattr(
        knowledge,
        "get_filters",
        lambda: {
            "database_include": [],
            "database_exclude": [],
            "task_include": [],
            "task_exclude": [],
        },
    )
    original = Record(
        "field",
        {
            "database": "shop",
            "table": "orders",
            "name": "amount",
            "comment": "exported",
        },
        "fields.json",
    )
    monkeypatch.setattr(
        knowledge,
        "read_sources",
        lambda *args: ([original], {"fields.json": "hash"}, None, {}),
    )
    monkeypatch.setattr(
        knowledge, "filter_records", lambda records, filters: (records, 0)
    )
    monkeypatch.setattr(knowledge, "validate_records", lambda records: None)
    monkeypatch.setattr(
        knowledge,
        "build_storage",
        lambda values, stats=None: nullcontext(
            StorageHandles("new", None, None, values)
        ),
    )
    monkeypatch.setattr(knowledge, "read_graph", lambda values: None)
    monkeypatch.setattr(knowledge, "read_vectors", lambda values: None)
    monkeypatch.setattr(knowledge, "prune_generations", lambda values: None)
    monkeypatch.setattr(knowledge, "delete_generation_data", lambda values: None)
    monkeypatch.setattr(relationships, "reconcile", lambda *args: None)
    observed = []

    def stage(records, generation, storage):
        observed.append(records[0].data["comment"])
        return StageResult()

    monkeypatch.setattr(stages, "STAGES", [stage])
    with sessions().begin() as db:
        db.add(MetadataOverride(target="field:shop.orders.amount", comment="corrected"))
    for _ in range(2):
        with sessions().begin() as db:
            build = Build(
                status="running",
                started_at="2026-01-01T00:00:00+00:00",
                counts="{}",
                build_type="full",
            )
            db.add(build)
            db.flush()
            build_id = build.id
        knowledge.run_build(build_id)
        with sessions()() as db:
            build = db.get(Build, build_id)
            assert build.status == "succeeded", build.error
    assert observed == ["corrected", "corrected"]
    assert original.data["comment"] == "exported"


def test_user_can_submit_table_correction_via_rest(client, monkeypatch):
    from semantica_dataagent import knowledge

    c, admin_token = client
    requester = _project_member(_requester(role="user", token="issue79-corrector"))
    monkeypatch.setattr(knowledge, "existing_refs", lambda refs: set(refs))
    monkeypatch.setattr(knowledge, "settings", lambda: {"knowledge_generation": "test"})
    monkeypatch.setattr(knowledge, "read_graph", lambda values: None)
    monkeypatch.setattr(
        knowledge, "execute", lambda *args: [{"node": {"comment": "原说明"}}]
    )
    response = c.post(
        "/api/v1/corrections",
        headers={"Authorization": f"Bearer {requester.token}"},
        json={"target": "table:shop.orders", "patch": "新说明", "reason": "口径更新"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    reviewed = c.get(
        "/api/v1/corrections", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert any(
        row["owner_id"] == requester.id and row["patch"] == "新说明"
        for row in reviewed.json()
    )
