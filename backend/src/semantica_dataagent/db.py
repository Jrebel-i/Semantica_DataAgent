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
