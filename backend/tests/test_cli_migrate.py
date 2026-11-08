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
    assert "0016" in output and "0033" in output
    assert _schema_version(database) == "0033"
    assert _kept_rows(database) == (("kept token", "admin"), ("kept value",))


def test_migrate_creates_pending_builds_table(database):
    assert cli.main(["migrate"]) == 0
    with sqlite3.connect(database) as db:
        columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(project_pending_builds)"
            ).fetchall()
        }
    assert columns == {"project_id", "due_at", "requested_by", "requested_at"}


def test_migrate_backs_up_before_upgrade_and_is_repeatable(database, capsys):
    _old_database(database)

    assert cli.main(["migrate"]) == 0
    backups = _backups(database)
    assert len(backups) == 1
    assert _schema_version(backups[0]) == "0016"
    assert _kept_rows(backups[0]) == _kept_rows(database)

    capsys.readouterr()
    assert cli.main(["migrate"]) == 0
    assert "0033" in capsys.readouterr().out
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
    assert _schema_version(database) == "0033"
    assert _kept_rows(database) == (("kept token", "admin"), ("kept value",))
    assert len(_backups(database)) == 1

    assert cli.main(["serve"]) == 0
    assert started == [True, True]
    assert len(_backups(database)) == 1
    assert "0033" in capsys.readouterr().out


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
    assert _schema_version(database) == "0033"
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
    assert _schema_version(database) == "0033"
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


def test_turn_switch_columns_migrate_without_rewriting_history(database):
    command.upgrade(_config(database), "0024")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO conversations (id, owner_id, title, agent, provider_id, model, created_at, updated_at, resumable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "owner",
                "旧会话",
                "pi",
                "provider",
                "model-a",
                "2026-01-01",
                "2026-01-01",
                1,
            ),
        )
        db.execute(
            "INSERT INTO turns (conversation_id, sequence, question, status, answer, sql, citations, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("old", 1, "旧问题", "completed", "旧回答", "[]", "[]", "2026-01-01"),
        )
    command.upgrade(_config(database), "0025")
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(turns)").fetchall()}
        assert {"provider_id", "model"} <= columns
        assert db.execute(
            "SELECT provider_id, model FROM turns WHERE conversation_id='old'"
        ).fetchone() == (None, None)
    command.downgrade(_config(database), "0024")
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(turns)").fetchall()}
        assert "provider_id" not in columns and "model" not in columns


def test_migrate_adds_empty_model_capabilities_for_existing_providers(database):
    command.upgrade(_config(database), "0025")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO providers "
            "(id, name, type, base_url, api_key, models, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "Legacy",
                "anthropic-messages",
                "https://legacy.example.test",
                None,
                '["legacy-model"]',
                "2026-01-01",
                "2026-01-01",
            ),
        )

    assert cli.main(["migrate"]) == 0

    assert _schema_version(database) == "0033"
    with sqlite3.connect(database) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(providers)").fetchall()
        }
        assert "model_capabilities" in columns
        assert db.execute(
            "SELECT model_capabilities FROM providers WHERE id = 'legacy'"
        ).fetchone() == (None,)


OLD_TABLES = (
    "tokens",
    "settings",
    "conversations",
    "turns",
    "correction_proposals",
    "builds",
    "knowledge_generations",
    "generation_pointer",
)


def _table_columns(path: Path, table: str) -> list[str]:
    with sqlite3.connect(path) as db:
        return [row[1] for row in db.execute(f"PRAGMA table_info({table})")]


def _table_rows(
    path: Path, table: str, columns: list[str] | None = None
) -> list[tuple]:
    with sqlite3.connect(path) as db:
        if columns is None:
            return sorted(db.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
        names = ", ".join(columns)
        return sorted(db.execute(f"SELECT {names} FROM {table}").fetchall(), key=repr)


def _previous_release_database(path: Path) -> None:
    command.upgrade(_config(path), "0026")
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO tokens (digest, name, role, id, created_at, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("admin-digest", "Admin", "admin", "admin-1", "2026-01-01", 0),
                ("user-1-digest", "User one", "user", "user-1", "2026-01-02", 0),
                ("user-2-digest", "User two", "user", "user-2", "2026-01-03", 0),
                ("revoked-digest", "Revoked", "user", "user-3", "2026-01-04", 1),
            ],
        )
        db.execute("INSERT INTO settings (key, value) VALUES ('kept', 'value')")
        db.execute(
            "INSERT INTO conversations (id, owner_id, title, agent, provider_id, model, "
            "created_at, updated_at, resumable) VALUES "
            "('conversation', 'user-1', '旧会话', 'pi', 'provider', 'model', "
            "'2026-01-01', '2026-01-01', 1)"
        )
        db.execute(
            "INSERT INTO turns (conversation_id, sequence, question, status, answer, "
            "sql, citations, started_at) VALUES "
            "('conversation', 1, '旧问题', 'completed', '旧回答', '[]', '[]', "
            "'2026-01-01')"
        )
        db.execute(
            "INSERT INTO correction_proposals (id, owner_id, target, patch, original, "
            "diff, reason, status, created_at) VALUES "
            "('proposal', 'user-1', 'field:shop.orders.id', 'new', 'old', 'diff', "
            "'reason', 'approved', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO builds (status, started_at, counts, generation) VALUES "
            "('succeeded', '2026-01-01', '{}', 'kept-generation')"
        )
        db.execute(
            "INSERT INTO knowledge_generations (name, created_at) VALUES "
            "('kept-generation', '2026-01-01')"
        )
        db.execute(
            "INSERT INTO generation_pointer (id, generation) VALUES (1, 'kept-generation')"
        )


def test_master_project_database_upgrades_to_audit_table(database):
    """The merged master schema (#175, 0029) upgrades without changing data."""
    command.upgrade(_config(database), "0029")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO tokens (digest, name, role, id, created_at, revoked) "
            "VALUES ('kept-digest', 'Kept user', 'user', 'kept-id', '2026-01-01', 0)"
        )
        db.execute(
            "INSERT INTO project_members (project_id, token_id, role) "
            "VALUES ('default', 'kept-id', 'member')"
        )
        db.execute(
            "INSERT INTO builds (project_id, status, started_at, counts, generation) "
            "VALUES ('default', 'succeeded', '2026-01-01', '{}', 'kept-generation')"
        )
        db.execute(
            "INSERT INTO knowledge_generations (name, project_id, created_at) "
            "VALUES ('kept-generation', 'default', '2026-01-01')"
        )
        db.execute(
            "UPDATE generation_pointer SET generation = 'kept-generation' "
            "WHERE project_id = 'default'"
        )
    before = {
        table: _table_rows(database, table)
        for table in (
            "tokens",
            "project_members",
            "projects",
            "builds",
            "knowledge_generations",
            "generation_pointer",
        )
    }

    assert cli.main(["migrate"]) == 0

    assert _schema_version(database) == "0033"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM project_audit").fetchone() == (0,)
        assert (
            db.execute("SELECT project_id, key, value FROM project_settings").fetchall()
            == []
        )
        assert {table: _table_rows(database, table) for table in before} == before
        assert (
            db.execute(
                "SELECT actor_name, action, detail FROM project_audit"
            ).fetchall()
            == []
        )
    backups = _backups(database)
    assert len(backups) == 1
    assert _schema_version(backups[0]) == "0029"


def test_project_migration_preserves_existing_data(database):
    _previous_release_database(database)
    before = {table: _table_rows(database, table) for table in OLD_TABLES}
    old_columns = {table: _table_columns(database, table) for table in OLD_TABLES}

    assert cli.main(["migrate"]) == 0

    assert _schema_version(database) == "0033"
    assert {
        table: _table_rows(database, table, old_columns[table]) for table in OLD_TABLES
    } == before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT id, name, type, status FROM projects").fetchall() == [
            ("default", "业务数据", "warehouse", "active")
        ]
        assert db.execute(
            "SELECT project_id, token_id, role FROM project_members ORDER BY token_id"
        ).fetchall() == [
            ("default", "user-1", "member"),
            ("default", "user-2", "member"),
        ]
        assert db.execute("SELECT DISTINCT project_id FROM builds").fetchall() == [
            ("default",)
        ]
        assert db.execute(
            "SELECT DISTINCT project_id FROM knowledge_generations"
        ).fetchall() == [("default",)]
        assert db.execute(
            "SELECT DISTINCT project_id FROM generation_pointer"
        ).fetchall() == [("default",)]
        assert (
            db.execute("SELECT project_id, key FROM project_settings").fetchall() == []
        )

    backups = _backups(database)
    assert len(backups) == 1
    assert _schema_version(backups[0]) == "0026"
    assert {table: _table_rows(backups[0], table) for table in OLD_TABLES} == before


def test_project_migration_is_repeatable(database):
    _previous_release_database(database)
    assert cli.main(["migrate"]) == 0
    assert cli.main(["migrate"]) == 0

    assert len(_backups(database)) == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM projects").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM project_members").fetchone() == (2,)


def test_empty_database_has_a_single_migration_head(database):
    from alembic.script import ScriptDirectory

    config = _config(database)
    assert ScriptDirectory.from_config(config).get_heads() == ["0033"]
    command.upgrade(config, "head")
    assert _schema_version(database) == "0033"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM projects").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM project_members").fetchone() == (0,)


def test_migrate_adds_null_effort_for_existing_turns(database):
    command.upgrade(_config(database), "0026")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO conversations (id, owner_id, title, agent, provider_id, model, created_at, updated_at, resumable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "owner",
                "旧会话",
                "pi",
                "provider",
                "model-a",
                "2026-01-01",
                "2026-01-01",
                1,
            ),
        )
        db.execute(
            "INSERT INTO turns (conversation_id, sequence, question, status, answer, sql, citations, started_at, provider_id, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                1,
                "旧问题",
                "completed",
                "旧回答",
                "[]",
                "[]",
                "2026-01-01",
                "provider",
                "model-a",
            ),
        )

    assert cli.main(["migrate"]) == 0

    assert _schema_version(database) == "0033"
    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(turns)").fetchall()}
        assert "effort" in columns
        assert db.execute(
            "SELECT effort FROM turns WHERE conversation_id='legacy'"
        ).fetchone() == (None,)


def test_migrate_backfills_agent_credential_project(database):
    command.upgrade(_config(database), "0031")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO agent_credentials "
            "(digest, owner_id, conversation_id, turn_id) VALUES (?, ?, ?, ?)",
            ("credential-digest", "owner", "conversation", 1),
        )

    assert cli.main(["migrate"]) == 0

    with sqlite3.connect(database) as db:
        project = db.execute(
            "SELECT project_id FROM agent_credentials WHERE digest = ?",
            ("credential-digest",),
        ).fetchone()
    assert project == ("default",)
    assert _schema_version(database) == "0033"


def test_migrated_builds_index_is_unique_per_project_and_status(database):
    assert cli.main(["migrate"]) == 0

    with sqlite3.connect(database) as db:
        index = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'one_running_build_per_project'"
        ).fetchone()
        assert index is not None, "one_running_build_per_project is missing"
        assert "WHERE status = 'running'" in index[0]
        insert = (
            "INSERT INTO builds "
            "(project_id, status, started_at, counts) VALUES (?, 'running', ?, '{}')"
        )
        db.execute(insert, ("default", "2026-01-01T00:00:00+00:00"))
        db.execute(insert, ("other", "2026-01-01T00:00:00+00:00"))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, ("default", "2026-01-01T00:00:01+00:00"))
