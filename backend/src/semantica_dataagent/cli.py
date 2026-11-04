import argparse
import os
import secrets

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from .db import Token, digest, sessions


def initialize() -> str:
    Fernet(os.environ["DATAAGENT_ENCRYPTION_KEY"].encode())
    config = Config()
    config.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "migrations")
    )
    config.set_main_option(
        "sqlalchemy.url",
        "sqlite:///" + os.environ["DATAAGENT_DB_PATH"].replace("%", "%%"),
    )
    command.upgrade(config, "head")
    with sessions().begin() as db:
        if db.query(Token).first():
            raise RuntimeError("Database already has a token")
        token = secrets.token_urlsafe(32)
        db.add(Token(digest=digest(token), name="initial admin", role="admin"))
    return token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "serve"])
    args = parser.parse_args()
    if args.command == "init":
        print(initialize())
    else:
        import uvicorn

        host, port = os.environ.get("DATAAGENT_LISTEN", "127.0.0.1:8000").rsplit(":", 1)
        uvicorn.run("semantica_dataagent.app:app", host=host, port=int(port))
