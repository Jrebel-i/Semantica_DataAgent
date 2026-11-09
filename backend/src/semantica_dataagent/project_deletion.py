"""Archive, restore and crash-safe permanent deletion for projects (#190).

Archive freezes every project-scoped entry point but keeps the project's rows
and storage.  Permanent deletion is only allowed for an archived non-default
project after the caller repeats its display name.  Deletion is split into
durable steps; after a crash the next start resumes at the recorded step.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import shutil
import threading
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, update

from . import auto_build, project_context
from .db import (
    AgentCredential,
    Build,
    Clarification,
    Conversation,
    CorrectionProposal,
    GenerationPointer,
    KnowledgeGap,
    KnowledgeGeneration,
    MetadataOverride,
    Project,
    ProjectDeletion,
    ProjectMember,
    ProjectPendingBuild,
    ProjectSetting,
    PullRecord,
    QueryExecution,
    Turn,
    TurnEvent,
    sessions,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_PROJECT_ID = "default"
ARCHIVED = project_context.ARCHIVED_DETAIL
STEP_ORDER = ("source", "neo4j", "milvus", "attachments", "database")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


def _reset_after_fork() -> None:
    global _locks_guard
    _locks_guard = threading.Lock()
    _locks.clear()


os.register_at_fork(after_in_child=_reset_after_fork)


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _steps(row: ProjectDeletion) -> list[dict]:
    try:
        value = json.loads(row.steps or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)]


def _write_steps(row: ProjectDeletion, steps: list[dict]) -> None:
    row.steps = json.dumps(steps, ensure_ascii=False)
    row.updated_at = datetime.now(UTC)


def is_project_generation(generation: str, project_id: str) -> bool:
    """Match only this project's ``<id>_`` generation namespace."""
    return generation.startswith(project_id + "_")


def is_project_collection(name: str, project_id: str) -> bool:
    """Match only this project's prefixed Milvus collection namespace."""
    return name.startswith(project_context.STORAGE_PREFIX + project_id + "_")


def _delete_source(project_id: str, settings: dict) -> None:
    root = project_context.projects_root().resolve()
    target = (root / project_id).resolve()
    if target.parent != root or target == root:
        raise RuntimeError("Refusing to delete outside the projects root")
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def _delete_neo4j(project_id: str, settings: dict) -> None:
    from . import knowledge
    from .stage_contract import delete_batches

    store = knowledge.graph(settings)
    try:
        delete_batches(
            lambda query, params: knowledge.execute(store, query, params),
            "MATCH (n:SDAEntity) WHERE n.generation STARTS WITH $prefix",
            "n",
            {"prefix": project_id + "_"},
            detach=True,
        )
    finally:
        store.close()


def _delete_milvus(project_id: str, settings: dict) -> None:
    from pymilvus import MilvusClient

    client = MilvusClient(uri=settings["milvus_uri"])
    try:
        for name in client.list_collections():
            if is_project_collection(name, project_id):
                client.drop_collection(name)
    finally:
        client.close()


def _call_attachment_hook(module_name: str, function_name: str, project_id, ids):
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False
    function = getattr(module, function_name, None)
    if not callable(function):
        return False
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "conversation_ids" in parameters:
        function(project_id=project_id, conversation_ids=ids)
    elif len(parameters) >= 2 or any(
        item.kind is inspect.Parameter.VAR_POSITIONAL for item in parameters.values()
    ):
        function(project_id, ids)
    else:
        for conversation_id in ids:
            function(conversation_id)
    return True


def _cleanup_attachments(project_id: str, conversation_ids: list[str]) -> None:
    """Invoke #188's attachment cleanup when it is installed, otherwise skip."""
    for module_name, function_name in (
        ("semantica_dataagent.attachments", "cleanup_project_attachments"),
        ("semantica_dataagent.attachments", "cleanup_project"),
        ("semantica_dataagent.attachments", "cleanup_conversation"),
        ("semantica_dataagent.attachment_cleanup", "cleanup_project"),
        (
            "semantica_dataagent.conversation_api",
            "cleanup_conversation_attachments",
        ),
    ):
        if _call_attachment_hook(
            module_name, function_name, project_id, conversation_ids
        ):
            return


def _conversation_ids(db, project_id: str) -> list[str]:
    return [
        row.id
        for row in db.query(Conversation.id)
        .filter(Conversation.project_id == project_id)
        .all()
    ]


def _delete_database(project_id: str, settings: dict) -> None:
    with sessions().begin() as db:
        conversation_ids = _conversation_ids(db, project_id)
        turn_ids = (
            [
                row.id
                for row in db.query(Turn.id)
                .filter(Turn.conversation_id.in_(conversation_ids))
                .all()
            ]
            if conversation_ids
            else []
        )
        if turn_ids:
            db.query(TurnEvent).filter(TurnEvent.turn_id.in_(turn_ids)).delete(
                synchronize_session=False
            )
            db.query(Turn).filter(Turn.id.in_(turn_ids)).delete(
                synchronize_session=False
            )
        if conversation_ids:
            db.query(Clarification).filter(
                Clarification.conversation_id.in_(conversation_ids)
            ).delete(synchronize_session=False)
            db.query(AgentCredential).filter(
                AgentCredential.conversation_id.in_(conversation_ids)
            ).delete(synchronize_session=False)
            db.query(QueryExecution).filter(
                QueryExecution.conversation_id.in_(conversation_ids)
            ).delete(synchronize_session=False)
        db.query(Conversation).filter(Conversation.project_id == project_id).delete(
            synchronize_session=False
        )
        db.query(Build).filter(Build.project_id == project_id).delete(
            synchronize_session=False
        )
        db.query(CorrectionProposal).filter(
            CorrectionProposal.project_id == project_id
        ).delete(synchronize_session=False)
        db.query(KnowledgeGap).filter(KnowledgeGap.project_id == project_id).delete(
            synchronize_session=False
        )
        db.query(MetadataOverride).filter(
            MetadataOverride.project_id == project_id
        ).delete(synchronize_session=False)
        db.query(ProjectPendingBuild).filter(
            ProjectPendingBuild.project_id == project_id
        ).delete(synchronize_session=False)
        db.query(ProjectSetting).filter(ProjectSetting.project_id == project_id).delete(
            synchronize_session=False
        )
        project_context.delete_plugin_configs(db, project_id)
        db.query(PullRecord).filter(PullRecord.project_id == project_id).delete(
            synchronize_session=False
        )
        db.query(ProjectMember).filter(ProjectMember.project_id == project_id).delete(
            synchronize_session=False
        )
        db.query(KnowledgeGeneration).filter(
            KnowledgeGeneration.project_id == project_id
        ).delete(synchronize_session=False)
        db.query(GenerationPointer).filter(
            GenerationPointer.project_id == project_id
        ).delete(synchronize_session=False)
        project = db.get(Project, project_id)
        if project is not None:
            db.delete(project)
        deletion = db.get(ProjectDeletion, project_id)
        if deletion is not None:
            steps = _steps(deletion)
            steps.append({"name": "database", "status": "succeeded", "at": _stamp()})
            _write_steps(deletion, steps)
            deletion.step = "done"
            deletion.status = "completed"
            deletion.error = None


def _step_runner(project_id: str, step: str) -> None:
    settings = None
    if step != "database":
        settings = _settings_for(project_id)
    if step == "attachments":
        with sessions()() as db:
            ids = _conversation_ids(db, project_id)
        _cleanup_attachments(project_id, ids)
    elif step == "source":
        _delete_source(project_id, settings)
    elif step == "neo4j":
        _delete_neo4j(project_id, settings)
    elif step == "milvus":
        _delete_milvus(project_id, settings)
    elif step == "database":
        _delete_database(project_id, settings or {})
    else:  # pragma: no cover - STEP_ORDER is fixed
        raise RuntimeError(f"Unknown deletion step {step}")


def _settings_for(project_id: str) -> dict:
    from . import knowledge

    return knowledge.settings(project_id)


def run_deletion(project_id: str) -> None:
    """Advance a persisted deletion until complete or a step fails."""
    with _lock(project_id):
        with sessions().begin() as db:
            deletion = db.get(ProjectDeletion, project_id)
            if deletion is None or deletion.status == "completed":
                return
            deletion.status = "deleting"
            deletion.error = None
            deletion.updated_at = datetime.now(UTC)
            completed = {
                item.get("name")
                for item in _steps(deletion)
                if item.get("status") == "succeeded"
            }
        for step in STEP_ORDER:
            if step in completed:
                continue
            with sessions().begin() as db:
                deletion = db.get(ProjectDeletion, project_id)
                if deletion is None or deletion.status == "completed":
                    return
                deletion.step = step
                deletion.status = "deleting"
                deletion.updated_at = datetime.now(UTC)
            try:
                _step_runner(project_id, step)
            except Exception as exc:
                LOGGER.exception("Project deletion %s failed at %s", project_id, step)
                with sessions().begin() as db:
                    deletion = db.get(ProjectDeletion, project_id)
                    if deletion is not None:
                        steps = _steps(deletion)
                        steps.append(
                            {
                                "name": step,
                                "status": "failed",
                                "at": _stamp(),
                                "error": str(exc)[:1000],
                            }
                        )
                        _write_steps(deletion, steps)
                        deletion.status = "failed"
                        deletion.step = step
                        deletion.error = str(exc)[:1000]
                raise
            if step == "database":
                return
            with sessions().begin() as db:
                deletion = db.get(ProjectDeletion, project_id)
                if deletion is None:
                    return
                steps = _steps(deletion)
                steps.append({"name": step, "status": "succeeded", "at": _stamp()})
                _write_steps(deletion, steps)
                deletion.step = STEP_ORDER[STEP_ORDER.index(step) + 1]


def archive_project(project_id: str, actor_id: str, actor_name: str) -> Project:
    from .project_audit import record_project_audit
    from .scheduler import scheduler

    with sessions().begin() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if project.status == project_context.ARCHIVED_STATUS:
            return project
        if project.status == project_context.DELETING_STATUS:
            raise HTTPException(status_code=409, detail="项目正在删除")
        running = (
            db.query(Build)
            .filter(
                func.coalesce(Build.project_id, DEFAULT_PROJECT_ID) == project_id,
                Build.status == "running",
            )
            .first()
        )
        if running is not None:
            raise HTTPException(status_code=409, detail="项目正在构建，暂时不能归档")
        project.status = project_context.ARCHIVED_STATUS
        auto_build.discard_pending_builds(project_id, db)
        db.execute(
            update(Build)
            .where(
                func.coalesce(Build.project_id, DEFAULT_PROJECT_ID) == project_id,
                Build.status == "queued",
            )
            .values(
                status="cancelled",
                finished_at=datetime.now(UTC).isoformat(),
                error="Build cancelled because the project was archived",
            )
        )
        record_project_audit(
            db,
            project_id=project_id,
            actor_id=actor_id,
            actor_name=actor_name,
            action="project.archive",
            detail={"name": project.name},
        )
        db.flush()
    scheduler.configure(0, project_id=project_id)
    return project


def restore_project(project_id: str, actor_id: str, actor_name: str) -> Project:
    from .project_api import _schedule_interval
    from .project_audit import record_project_audit
    from .scheduler import scheduler

    with sessions().begin() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        if project.status == project_context.DELETING_STATUS:
            raise HTTPException(status_code=409, detail="项目正在删除")
        if project.status == project_context.ACTIVE_STATUS:
            return project
        project.status = project_context.ACTIVE_STATUS
        record_project_audit(
            db,
            project_id=project_id,
            actor_id=actor_id,
            actor_name=actor_name,
            action="project.restore",
            detail={"name": project.name},
        )
        db.flush()
        interval = _schedule_interval(db, project_id)
    scheduler.configure(interval, project_id=project_id)
    return project


def prepare_deletion(project_id: str, name: str, actor_id: str, actor_name: str) -> str:
    """Validate and persist the deletion request; returns completed/active."""
    from .project_audit import record_project_audit

    with sessions().begin() as db:
        deletion = db.get(ProjectDeletion, project_id)
        project = db.get(Project, project_id)
        if project is None:
            if deletion is not None and deletion.status == "completed":
                return "completed"
            if deletion is not None:
                return "active"
            raise HTTPException(status_code=404, detail="Project not found")
        stale = deletion is not None and deletion.status == "completed"
        if project.id == DEFAULT_PROJECT_ID:
            raise HTTPException(status_code=409, detail="默认项目不可删除")
        if project.status not in (
            project_context.ARCHIVED_STATUS,
            project_context.DELETING_STATUS,
        ):
            raise HTTPException(status_code=409, detail="项目未归档，不能删除")
        if name != project.name:
            raise HTTPException(status_code=422, detail="项目名不匹配")
        if deletion is None:
            deletion = ProjectDeletion(
                project_id=project_id,
                project_name=project.name,
                status="deleting",
                step="begin",
                steps="[]",
                requested_by=actor_id,
            )
            db.add(deletion)
            record_project_audit(
                db,
                project_id=project_id,
                actor_id=actor_id,
                actor_name=actor_name,
                action="project.delete",
                detail={"name": project.name},
            )
        else:
            if stale:
                deletion.project_name = project.name
                deletion.step = "begin"
                deletion.steps = "[]"
                deletion.requested_by = actor_id
                deletion.created_at = datetime.now(UTC)
                record_project_audit(
                    db,
                    project_id=project_id,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    action="project.delete",
                    detail={"name": project.name},
                )
            deletion.status = "deleting"
            deletion.error = None
            deletion.updated_at = datetime.now(UTC)
        project.status = project_context.DELETING_STATUS
    return "active"


def recover_project_deletions() -> None:
    """Resume every deletion left running or failed by a previous process."""
    with sessions()() as db:
        project_ids = [
            row.project_id
            for row in db.query(ProjectDeletion)
            .join(Project, Project.id == ProjectDeletion.project_id)
            .filter(ProjectDeletion.status.in_(("deleting", "failed")))
            .filter(Project.status == project_context.DELETING_STATUS)
            .all()
        ]
    for project_id in project_ids:
        try:
            run_deletion(project_id)
        except Exception:
            LOGGER.exception("Could not resume project deletion %s", project_id)
