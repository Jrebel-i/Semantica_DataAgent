"""Generation retention and interrupt cleanup stay inside one project (#175)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from semantica_dataagent.cli import initialize

    initialize()


def _project(project_id: str, name: str, generation: str) -> None:
    from semantica_dataagent.db import GenerationPointer, Project, sessions

    with sessions().begin() as db:
        db.add(Project(id=project_id, name=name, type="document", status="active"))
        db.add(GenerationPointer(project_id=project_id, generation=generation))


def _fetch(sql: str, params: tuple = ()) -> list[tuple]:
    import sqlite3

    with sqlite3.connect(os.environ["DATAAGENT_DB_PATH"]) as db:
        return db.execute(sql, params).fetchall()


def test_pruning_one_project_never_touches_another(tmp_path, monkeypatch):
    from semantica_dataagent import knowledge, project_context
    from semantica_dataagent.db import KnowledgeGeneration, sessions

    _seed(tmp_path, monkeypatch)
    _project("alpha", "Alpha", "alpha_gen2")
    _project("beta", "Beta", "beta_gen2")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with sessions().begin() as db:
        for project_id in ("alpha", "beta"):
            for index in range(3):
                db.add(
                    KnowledgeGeneration(
                        name=f"{project_id}_gen{index}",
                        project_id=project_id,
                        created_at=(start + timedelta(days=index)).isoformat(),
                    )
                )

    deleted: list[str] = []
    monkeypatch.setattr(
        knowledge,
        "delete_generation_data",
        lambda values: deleted.append(project_context.from_values(values).generation),
    )
    knowledge.prune_generations(project_context.current_project("alpha").values)
    assert deleted == ["alpha_gen0"]
    assert [
        row[0]
        for row in _fetch(
            "SELECT name FROM knowledge_generations "
            "WHERE project_id = 'alpha' ORDER BY name"
        )
    ] == ["alpha_gen1", "alpha_gen2"]
    assert [
        row[0]
        for row in _fetch(
            "SELECT name FROM knowledge_generations "
            "WHERE project_id = 'beta' ORDER BY name"
        )
    ] == ["beta_gen0", "beta_gen1", "beta_gen2"]
    assert _fetch(
        "SELECT generation FROM generation_pointer WHERE project_id = 'beta'"
    ) == [("beta_gen2",)]


def test_interrupted_cleanup_keeps_another_projects_current_generation(
    tmp_path, monkeypatch
):
    from semantica_dataagent import knowledge, project_context
    from semantica_dataagent.db import Build, sessions

    _seed(tmp_path, monkeypatch)
    _project("alpha", "Alpha", "alpha_current")
    _project("beta", "Beta", "beta_current")
    with sessions().begin() as db:
        db.add_all(
            [
                Build(
                    status="failed",
                    error="Build interrupted by application shutdown",
                    started_at="2026-01-01",
                    counts="{}",
                    build_type="full",
                    generation="alpha_orphan",
                    project_id="alpha",
                ),
                Build(
                    status="failed",
                    error="Build interrupted by application shutdown",
                    started_at="2026-01-01",
                    counts="{}",
                    build_type="full",
                    generation="beta_current",
                    project_id="beta",
                ),
            ]
        )

    deleted: list[str] = []
    monkeypatch.setattr(
        knowledge,
        "delete_generation_data",
        lambda values: deleted.append(project_context.from_values(values).generation),
    )
    knowledge.cleanup_interrupted_generations()
    assert deleted == ["alpha_orphan"]
    assert _fetch(
        "SELECT generation FROM generation_pointer WHERE project_id = 'beta'"
    ) == [("beta_current",)]


def test_generation_names_and_collections_do_not_collide_between_projects():
    from semantica_dataagent import project_context

    short = project_context.from_values(
        {"knowledge_generation": "a_initial", "_project_id": "a"}
    )
    long = project_context.from_values(
        {"knowledge_generation": "ab_initial", "_project_id": "ab"}
    )
    short_generation = short.new_generation()
    long_generation = long.new_generation()
    assert short_generation.startswith("a_")
    assert long_generation.startswith("ab_")
    assert short.collection_name(short_generation) != long.collection_name(
        long_generation
    )
    assert short.collection_name(short_generation).startswith("sda_a_")
    assert long.collection_name(long_generation).startswith("sda_ab_")
