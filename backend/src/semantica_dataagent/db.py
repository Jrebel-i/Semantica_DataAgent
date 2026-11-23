import hashlib
import os
import uuid
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy import Index, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Token(Base):
    __tablename__ = "tokens"
    digest: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    role: Mapped[str]
    id: Mapped[str] = mapped_column(unique=True, default=lambda: uuid.uuid4().hex)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    last_used_at: Mapped[datetime | None]
    revoked: Mapped[bool] = mapped_column(default=False)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    type: Mapped[str]
    status: Mapped[str] = mapped_column(default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[str] = mapped_column(primary_key=True)
    token_id: Mapped[str] = mapped_column(primary_key=True)
    role: Mapped[str]


class ProjectSetting(Base):
    __tablename__ = "project_settings"
    project_id: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


class ProjectPendingBuild(Base):
    __tablename__ = "project_pending_builds"
    project_id: Mapped[str] = mapped_column(primary_key=True)
    due_at: Mapped[str]
    requested_by: Mapped[str]
    requested_at: Mapped[str]


class ProjectAudit(Base):
    __tablename__ = "project_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(index=True)
    actor_id: Mapped[str]
    actor_name: Mapped[str]
    action: Mapped[str]
    detail: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class ProjectDeletion(Base):
    """Durable progress for a multi-step permanent project deletion."""

    __tablename__ = "project_deletions"
    project_id: Mapped[str] = mapped_column(primary_key=True)
    project_name: Mapped[str]
    status: Mapped[str] = mapped_column(default="deleting")
    step: Mapped[str] = mapped_column(default="begin")
    steps: Mapped[str] = mapped_column(default="[]", server_default="[]")
    error: Mapped[str | None]
    requested_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class AgentCredential(Base):
    __tablename__ = "agent_credentials"
    digest: Mapped[str] = mapped_column(primary_key=True)
    owner_id: Mapped[str]
    conversation_id: Mapped[str]
    turn_id: Mapped[int]
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


_factories = {}
_factory_lock = Lock()
_stale_factories = []


def _reset_after_fork() -> None:
    """Give the child fresh locks and engines instead of the parent's state.

    A forked child inherits locks held by unrelated parent threads.  Reusing the
    parent's SQLAlchemy pool can therefore wait forever on a mutex that no
    thread in the child will ever release.  Keep the old engines referenced so
    their connections are not finalized in the child, and build a new engine on
    the first ``sessions()`` call.
    """
    global _factory_lock
    _factory_lock = Lock()
    _stale_factories.extend(_factories.values())
    _factories.clear()


os.register_at_fork(after_in_child=_reset_after_fork)


def _configure_sqlite_engine(engine, path: str) -> None:
    """Apply busy timeout, normal sync, and persistent WAL mode to SQLite engines."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    if path != ":memory:" and not path.startswith("file:"):
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.commit()


def _sessions_for_path(path: str):
    with _factory_lock:
        if path not in _factories:
            engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 30})
            _configure_sqlite_engine(engine, path)
            _factories[path] = sessionmaker(engine)
        return _factories[path]


def sessions():
    return _sessions_for_path(os.environ["DATAAGENT_DB_PATH"])


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Build(Base):
    __tablename__ = "builds"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    status: Mapped[str]
    started_at: Mapped[str]
    finished_at: Mapped[str | None]
    counts: Mapped[str]
    error: Mapped[str | None]
    parse_failures: Mapped[str] = mapped_column(default="[]", server_default="[]")
    logs: Mapped[str | None]
    build_type: Mapped[str | None]
    correction_proposal_id: Mapped[str | None]
    triggered_by: Mapped[str | None]
    source_commit: Mapped[str | None]
    generation: Mapped[str | None]
    source_hashes: Mapped[str | None]
    config_hash: Mapped[str | None]
    affected_files: Mapped[str | None]
    dimension_tables: Mapped[str | None]
    generated_files: Mapped[str | None]
    stale_overrides: Mapped[str] = mapped_column(default="[]", server_default="[]")
    entity_hashes: Mapped[str | None]
    fallback_reason: Mapped[str | None]


class PluginConfig(Base):
    __tablename__ = "plugin_config"
    project_id: Mapped[str] = mapped_column(
        primary_key=True, default="default", server_default="default"
    )
    category: Mapped[str] = mapped_column(primary_key=True)
    plugin_id: Mapped[str] = mapped_column(primary_key=True)
    enabled: Mapped[bool]
    parameters: Mapped[str]


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str]
    type: Mapped[str]
    base_url: Mapped[str]
    api_key: Mapped[str | None]
    models: Mapped[str]
    model_capabilities: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id: Mapped[str]
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    title: Mapped[str]
    agent: Mapped[str]
    provider_id: Mapped[str]
    model: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    resumable: Mapped[bool] = mapped_column(default=True, server_default="1")
    pinned_at: Mapped[datetime | None]
    archived_at: Mapped[datetime | None]


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str]
    provider_id: Mapped[str | None]
    model: Mapped[str | None]
    effort: Mapped[str | None]
    sequence: Mapped[int]
    question: Mapped[str]
    status: Mapped[str]
    answer: Mapped[str]
    sql: Mapped[str]
    citations: Mapped[str]
    error: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    finished_at: Mapped[datetime | None]


class TurnEvent(Base):
    __tablename__ = "turn_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    turn_id: Mapped[int]
    seq: Mapped[int]
    type: Mapped[str]
    data: Mapped[str]


class KnowledgeGeneration(Base):
    __tablename__ = "knowledge_generations"
    name: Mapped[str] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    build_id: Mapped[int | None]
    created_at: Mapped[str | None]
    deleting: Mapped[bool] = mapped_column(default=False, server_default="0")
    is_candidate: Mapped[bool] = mapped_column(default=False, server_default="0")


class GenerationPointer(Base):
    __tablename__ = "generation_pointer"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    generation: Mapped[str]


def generation_pointer(db, project_id: str = "default"):
    return (
        db.query(GenerationPointer)
        .filter(GenerationPointer.project_id == project_id)
        .first()
    )


class Clarification(Base):
    __tablename__ = "clarifications"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id: Mapped[str]
    conversation_id: Mapped[str | None]
    questions: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class KnowledgeGap(Base):
    __tablename__ = "knowledge_gaps"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    owner_id: Mapped[str]
    question: Mapped[str]
    missing: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class CorrectionProposal(Base):
    __tablename__ = "correction_proposals"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    owner_id: Mapped[str]
    conversation_id: Mapped[str | None]
    source: Mapped[str] = mapped_column(default="manual", server_default="manual")
    target_kind: Mapped[str] = mapped_column(
        default="document", server_default="document"
    )
    target: Mapped[str]
    patch: Mapped[str]
    original: Mapped[str]
    diff: Mapped[str]
    reason: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")
    original_patch: Mapped[str | None]
    structured_patch: Mapped[str | None]
    gate_report: Mapped[str | None]
    source_directory: Mapped[str | None]
    source_path: Mapped[str | None]
    reviewer_name: Mapped[str | None]
    commit_sha: Mapped[str | None]
    revert_commit_sha: Mapped[str | None]
    reviewer_id: Mapped[str | None]
    reviewed_at: Mapped[datetime | None]
    reverted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class MetadataOverride(Base):
    __tablename__ = "metadata_overrides"
    project_id: Mapped[str] = mapped_column(
        primary_key=True, default="default", server_default="default"
    )
    target: Mapped[str] = mapped_column(primary_key=True)
    comment: Mapped[str]


class PersonalDtbToken(Base):
    __tablename__ = "personal_dtb_tokens"
    owner_id: Mapped[str] = mapped_column(primary_key=True)
    encrypted_token: Mapped[str]


class QueryExecution(Base):
    __tablename__ = "query_executions"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id: Mapped[str]
    conversation_id: Mapped[str | None]
    turn_id: Mapped[int | None]
    plan_data: Mapped[str | None]
    sql: Mapped[str]
    status: Mapped[str]
    rows: Mapped[str | None]
    query_key: Mapped[str | None]
    row_count: Mapped[int | None]
    elapsed_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class QueryAudit(Base):
    __tablename__ = "query_audit"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id: Mapped[str]
    sql: Mapped[str]
    status: Mapped[str]
    kind: Mapped[str] = mapped_column(default="query", server_default="query")
    row_count: Mapped[int | None]
    elapsed_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class PullRecord(Base):
    __tablename__ = "pull_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    instance_id: Mapped[str | None]
    plugin_id: Mapped[str | None]
    trigger_type: Mapped[str] = mapped_column(default="manual", server_default="manual")
    status: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    tables: Mapped[int] = mapped_column(default=0)
    tasks: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None]
    skipped_reason: Mapped[str | None]
    warnings: Mapped[str] = mapped_column(default="[]", server_default="[]")
    commit: Mapped[str | None]
    build_id: Mapped[int | None]
    source_directory: Mapped[str | None]
    operation_id: Mapped[str | None]


class ProjectPullInstance(Base):
    """Per-project configured puller instance with isolated namespace and schedule."""

    __tablename__ = "project_pull_instances"
    __table_args__ = (Index("ix_project_pull_instances_project", "project_id"),)

    project_id: Mapped[str] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(primary_key=True)
    plugin_id: Mapped[str]
    display_name: Mapped[str]
    enabled: Mapped[bool] = mapped_column(default=True, server_default="1")
    parameters: Mapped[str] = mapped_column(default="{}", server_default="{}")
    encrypted_secrets: Mapped[str] = mapped_column(default="{}", server_default="{}")
    schedule_cron: Mapped[str | None]
    timezone: Mapped[str] = mapped_column(
        default="Asia/Shanghai", server_default="Asia/Shanghai"
    )
    catalog_prefix: Mapped[str | None]
    task_namespace: Mapped[str | None]
    drop_threshold: Mapped[float] = mapped_column(default=0.5, server_default="0.5")
    last_table_count: Mapped[int | None]
    last_triggered_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class InteractionTrajectory(Base):
    """Sanitized, untruncated interaction trajectory persisted per project."""

    __tablename__ = "interaction_trajectories"
    __table_args__ = (
        Index(
            "ix_interaction_trajectories_project_recorded_id",
            "project_id",
            "recorded_at",
            "id",
        ),
        Index(
            "ix_interaction_trajectories_project_id_cursor",
            "project_id",
            "id",
        ),
        Index(
            "ix_interaction_trajectories_conv_turn",
            "conversation_id",
            "turn_id",
        ),
        Index(
            "ix_interaction_trajectories_local_window",
            "project_id",
            "token_id",
            "source_type",
            "last_activity_at",
        ),
        Index(
            "ix_interaction_trajectories_session_key",
            "session_key",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    source_type: Mapped[str]
    session_key: Mapped[str]
    conversation_id: Mapped[str | None]
    turn_id: Mapped[int | None]
    token_id: Mapped[str | None]
    langfuse_trace_id: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="running", server_default="running")
    question: Mapped[str] = mapped_column(default="", server_default="")
    answer: Mapped[str] = mapped_column(default="", server_default="")
    tool_calls: Mapped[str] = mapped_column(default="[]", server_default="[]")
    matched_refs: Mapped[str] = mapped_column(default="[]", server_default="[]")
    clarifications: Mapped[str] = mapped_column(default="[]", server_default="[]")
    sql_validations: Mapped[str] = mapped_column(default="[]", server_default="[]")
    final_sql: Mapped[str] = mapped_column(default="[]", server_default="[]")
    citations: Mapped[str] = mapped_column(default="[]", server_default="[]")
    window_started_at: Mapped[datetime | None]
    last_activity_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC)
    )
    recorded_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class EvolutionCheckpoint(Base):
    """Per-project trajectory checkpoint advanced only on accepted evolution runs."""

    __tablename__ = "evolution_checkpoints"

    project_id: Mapped[str] = mapped_column(primary_key=True)
    checkpoint_trajectory_id: Mapped[int | None]
    checkpoint_at: Mapped[datetime | None]
    last_run_id: Mapped[str | None]
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class EvolutionRun(Base):
    """Persisted state machine record for one autonomous or manual evolution run."""

    __tablename__ = "evolution_runs"
    __table_args__ = (
        Index(
            "ix_evolution_runs_project_created",
            "project_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    trigger_type: Mapped[str] = mapped_column(default="manual", server_default="manual")
    status: Mapped[str] = mapped_column(default="running", server_default="running")
    checkpoint_before_trajectory_id: Mapped[int | None]
    checkpoint_after_trajectory_id: Mapped[int | None]
    frozen_max_trajectory_id: Mapped[int | None]
    diagnosis_trajectory_ids: Mapped[str] = mapped_column(
        default="[]", server_default="[]"
    )
    reserve_trajectory_ids: Mapped[str] = mapped_column(
        default="[]", server_default="[]"
    )
    signal_summary: Mapped[str] = mapped_column(default="{}", server_default="{}")
    max_rounds: Mapped[int] = mapped_column(default=3, server_default="3")
    max_eval_cases: Mapped[int] = mapped_column(default=20, server_default="20")
    rounds_completed: Mapped[int] = mapped_column(default=0, server_default="0")
    stop_reason: Mapped[str | None]
    error: Mapped[str | None]
    proposal_id: Mapped[str | None]
    created_by: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class EvolutionRound(Base):
    """Single-hypothesis round record within an evolution run."""

    __tablename__ = "evolution_rounds"
    __table_args__ = (
        Index(
            "ix_evolution_rounds_run_round",
            "run_id",
            "round_number",
        ),
        Index(
            "ix_evolution_rounds_project",
            "project_id",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str]
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    round_number: Mapped[int]
    signal_kind: Mapped[str]
    hypothesis: Mapped[str]
    evidence_trajectory_ids: Mapped[str] = mapped_column(
        default="[]", server_default="[]"
    )
    target: Mapped[str] = mapped_column(default="", server_default="")
    target_kind: Mapped[str] = mapped_column(
        default="override", server_default="override"
    )
    candidate_change: Mapped[str] = mapped_column(default="{}", server_default="{}")
    l1_gate_report: Mapped[str | None]
    l2_gate_report: Mapped[str | None]
    decision: Mapped[str] = mapped_column(default="running", server_default="running")
    reject_reason: Mapped[str | None]
    proposal_id: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    finished_at: Mapped[datetime | None]


class DiscoveryRun(Base):
    """Offline cold-start knowledge discovery run state and aggregate metrics (#304)."""

    __tablename__ = "discovery_runs"
    __table_args__ = (
        Index(
            "ix_discovery_runs_project_created",
            "project_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    status: Mapped[str] = mapped_column(default="running", server_default="running")
    mine_query_history: Mapped[bool] = mapped_column(default=True, server_default="1")
    probe_relationships: Mapped[bool] = mapped_column(default=True, server_default="1")
    seed_eval_drafts: Mapped[bool] = mapped_column(default=False, server_default="0")
    config_snapshot: Mapped[str] = mapped_column(default="{}", server_default="{}")
    queries_fetched: Mapped[int] = mapped_column(default=0, server_default="0")
    templates_clustered: Mapped[int] = mapped_column(default=0, server_default="0")
    candidates_recalled: Mapped[int] = mapped_column(default=0, server_default="0")
    candidates_probed: Mapped[int] = mapped_column(default=0, server_default="0")
    candidates_passed: Mapped[int] = mapped_column(default=0, server_default="0")
    candidates_skipped: Mapped[int] = mapped_column(default=0, server_default="0")
    candidates_failed: Mapped[int] = mapped_column(default=0, server_default="0")
    proposals_created: Mapped[int] = mapped_column(default=0, server_default="0")
    eval_drafts_created: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None]
    created_by: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    finished_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class DiscoveryCandidate(Base):
    """Aggregated candidate outcome record produced by a discovery run (#304)."""

    __tablename__ = "discovery_candidates"
    __table_args__ = (
        Index(
            "ix_discovery_candidates_run_created",
            "run_id",
            "created_at",
        ),
        Index(
            "ix_discovery_candidates_project",
            "project_id",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str]
    project_id: Mapped[str] = mapped_column(default="default", server_default="default")
    candidate_kind: Mapped[str]
    target: Mapped[str] = mapped_column(default="", server_default="")
    evidence_json: Mapped[str] = mapped_column(default="{}", server_default="{}")
    decision: Mapped[str]
    reason: Mapped[str | None]
    proposal_id: Mapped[str | None]
    eval_question_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
