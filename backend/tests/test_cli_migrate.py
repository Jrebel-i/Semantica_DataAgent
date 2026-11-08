import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from semantica_dataagent import cli


def _config(path: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(cli.__file__).parent / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _schema_version(path: Path) -> str | None:
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def _old_database(path: Path) -> None:
    command.upgrade(_config(path), "0016")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO tokens (digest, name, role, id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("kept-digest", "kept token", "admin", "kept-id", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("kept-setting", "kept value"),
        )


def _kept_rows(path: Path) -> tuple[tuple, tuple]:
    with sqlite3.connect(path) as db:
        token = db.execute(
            "SELECT name, role FROM tokens WHERE digest = 'kept-digest'"
        ).fetchone()
        setting = db.execute(
            "SELECT value FROM settings WHERE key = 'kept-setting'"
        ).fetchone()
    return token, setting


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "app.db"
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(path))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return path


def test_migrate_upgrades_from_0016_and_keeps_data(database, capsys):
    _old_database(database)

    assert cli.main(["migrate"]) == 0

    output = capsys.readouterr().out
    assert "0016" in output and "0024" in output
    assert _schema_version(database) == "0024"
    assert _kept_rows(database) == (("kept token", "admin"), ("kept value",))


def test_migrate_backs_up_before_upgrade_and_is_repeatable(database, capsys):
    _old_database(database)

    assert cli.main(["migrate"]) == 0
    backups = _backups(database)
    assert len(backups) == 1
    assert _schema_version(backups[0]) == "0016"
    assert _kept_rows(backups[0]) == _kept_rows(database)

    capsys.readouterr()
    assert cli.main(["migrate"]) == 0
    assert "0024" in capsys.readouterr().out
    assert len(_backups(database)) == 1


def test_migrate_keeps_only_three_backups(database):
    _old_database(database)
    for index in range(4):
        name = f"{database.name}.bak-2026010{index}T000000.000000Z"
        (database.parent / name).touch()

    assert cli.main(["migrate"]) == 0

    backups = _backups(database)
    oldest = f"{database.name}.bak-20260100T000000.000000Z"
    assert len(backups) == 3
    assert all(backup.name != oldest for backup in backups)


def test_serve_migrates_before_starting(database, capsys, monkeypatch):
    _old_database(database)
    started = []
    monkeypatch.setattr(cli, "_serve", lambda: started.append(True))

    assert cli.main(["serve"]) == 0
    assert started == [True]
    assert _schema_version(database) == "0024"
    assert _kept_rows(database) == (("kept token", "admin"), ("kept value",))
    assert len(_backups(database)) == 1

    assert cli.main(["serve"]) == 0
    assert started == [True, True]
    assert len(_backups(database)) == 1
    assert "0024" in capsys.readouterr().out


def test_serve_does_not_start_when_migration_fails(database, capsys, monkeypatch):
    database.write_bytes(b"not a sqlite database")
    started = []
    monkeypatch.setattr(cli, "_serve", lambda: started.append(True))

    assert cli.main(["serve"]) != 0

    assert started == []
    error = capsys.readouterr().err
    assert str(database) in error
    assert "unknown" in error.lower()
    assert "not a database" in error


def test_init_still_initializes_an_empty_database(database):
    token = cli.initialize()

    assert token
    assert _schema_version(database) == "0024"
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT name, role FROM tokens").fetchone()
    assert row == ("initial admin", "admin")


def test_serve_upgrades_0023_conversations_and_backs_up(database, monkeypatch):
    command.upgrade(_config(database), "0023")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO conversations (id, owner_id, title, agent, provider_id, model, created_at, updated_at, resumable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "owner",
                "旧会话",
                "pi",
                "provider",
                "model",
                "2026-01-01",
                "2026-01-01",
                1,
            ),
        )
    monkeypatch.setattr(cli, "_serve", lambda: None)
    assert cli.main(["serve"]) == 0
    assert _schema_version(database) == "0024"
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT title, pinned_at, archived_at FROM conversations WHERE id='old'"
        ).fetchone() == ("旧会话", None, None)
    backups = _backups(database)
    assert len(backups) == 1
    assert _schema_version(backups[0]) == "0023"
    command.downgrade(_config(database), "0023")
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT title FROM conversations WHERE id='old'"
        ).fetchone() == ("旧会话",)
