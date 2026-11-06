"""Verify serve upgrades an older SQLite database before accepting REST traffic."""

import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config

from semantica_dataagent import cli


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _version(path: Path) -> str | None:
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


def _environment(process: subprocess.Popen) -> dict[str, str]:
    entries = Path(f"/proc/{process.pid}/environ").read_bytes().decode().split("\0")
    return dict(item.split("=", 1) for item in entries if "=" in item)


def _restart(ctx, env: dict[str, str]) -> None:
    runtime = ctx.runtime
    process = runtime.server
    ctx.require(process is not None, "serve process is missing")
    runtime.server = subprocess.Popen(
        process.args,
        cwd=Path(cli.__file__).resolve().parents[3],
        env=env,
        stdout=runtime.service_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + ctx.config.start_timeout
    while time.monotonic() < deadline:
        if runtime.server.poll() is not None:
            log = Path(runtime.service_log).read_text(errors="replace")[-3000:]
            raise AssertionError(f"serve exited after restart:\n{log}")
        try:
            response = ctx.rest.get("/api/v1/me", timeout=2)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    log = Path(runtime.service_log).read_text(errors="replace")[-3000:]
    raise AssertionError(f"serve did not become ready after restart:\n{log}")


def check_serve_migrates_old_database(ctx):
    runtime = ctx.runtime
    database = Path(runtime.db_path)
    created = ctx.rest.post(
        "/api/v1/tokens", json={"name": "issue 98 kept token", "role": "user"}
    )
    ctx.require(created.status_code == 201, created.text)
    kept = created.json()
    headers = {"Authorization": f"Bearer {kept['token']}"}

    process = runtime.server
    ctx.require(process is not None, "serve process is missing")
    env = _environment(process)
    process.kill()
    process.wait(timeout=30)
    command.downgrade(_config(database), "0016")
    ctx.require(_version(database) == "0016", "database did not downgrade to 0016")
    ctx.require(not _backups(database), "unexpected backup before restart")

    _restart(ctx, env)

    ctx.require(_version(database) == "0020", "serve did not migrate the database")
    me = ctx.rest.get("/api/v1/me", headers=headers)
    ctx.require(me.status_code == 200, f"token did not survive migration: {me.text}")
    listing = ctx.rest.get("/api/v1/tokens")
    ctx.require(listing.status_code == 200, listing.text)
    ctx.require(
        any(row["id"] == kept["id"] for row in listing.json()),
        "existing token row was not preserved",
    )
    backups = _backups(database)
    ctx.require(len(backups) == 1, f"expected one pre-migration backup: {backups}")
    ctx.require(_version(backups[0]) == "0016", "backup is not at the old version")
    with sqlite3.connect(backups[0]) as db:
        row = db.execute(
            "SELECT name FROM tokens WHERE id = ?", (kept["id"],)
        ).fetchone()
    ctx.require(
        row == ("issue 98 kept token",), "backup does not contain existing data"
    )

    log = Path(runtime.service_log).read_text(errors="replace")
    ctx.require("version before: 0016" in log, "migration did not log the old version")
    ctx.require("version after: 0020" in log, "migration did not log the new version")

    _restart(ctx, env)
    ctx.require(_version(database) == "0020", "database left head after restart")
    ctx.require(len(_backups(database)) == 1, "restart at head created another backup")
    return True
