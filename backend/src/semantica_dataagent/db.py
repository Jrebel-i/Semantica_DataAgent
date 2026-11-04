import hashlib
import os

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


def sessions():
    path = os.environ["DATAAGENT_DB_PATH"]
    return sessionmaker(create_engine(f"sqlite:///{path}"))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
