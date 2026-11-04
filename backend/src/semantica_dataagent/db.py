import hashlib
import os
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
