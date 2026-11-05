import hashlib
import os
import uuid
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy import create_engine
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


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


_factories = {}
_factory_lock = Lock()


def _sessions_for_path(path: str):
    with _factory_lock:
        if path not in _factories:
            _factories[path] = sessionmaker(create_engine(f"sqlite:///{path}"))
        return _factories[path]


def sessions():
    return _sessions_for_path(os.environ["DATAAGENT_DB_PATH"])


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Build(Base):
    __tablename__ = "builds"
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str]
    started_at: Mapped[str]
    finished_at: Mapped[str | None]
    counts: Mapped[str]
    error: Mapped[str | None]
    parse_failures: Mapped[str] = mapped_column(default="[]", server_default="[]")
    logs: Mapped[str | None]
    build_type: Mapped[str | None]
    triggered_by: Mapped[str | None]
    source_commit: Mapped[str | None]
    generation: Mapped[str | None]
    source_hashes: Mapped[str | None]
    config_hash: Mapped[str | None]
    affected_files: Mapped[str | None]
    dimension_tables: Mapped[str | None]
    generated_files: Mapped[str | None]


class PluginConfig(Base):
    __tablename__ = "plugin_config"
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
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id: Mapped[str]
    title: Mapped[str]
    agent: Mapped[str]
    provider_id: Mapped[str]
    model: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str]
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
    build_id: Mapped[int | None]
    created_at: Mapped[str | None]


class GenerationPointer(Base):
    __tablename__ = "generation_pointer"
    id: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[str]
