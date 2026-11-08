"""The project-context entry point resolves explicit projects and keeps defaults."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from semantica_dataagent import project_context


def test_resolution_keeps_request_contexts_on_the_default_project():
    assert project_context.resolve_project_id() == "default"
    assert (
        project_context.resolve_project_id(request=SimpleNamespace(project_id="p1"))
        == "default"
    )
    assert (
        project_context.resolve_project_id(token=SimpleNamespace(id="t1")) == "default"
    )
    assert (
        project_context.resolve_project_id(conversation={"project_id": "p2"})
        == "default"
    )


def test_unknown_explicit_project_is_rejected(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from semantica_dataagent.cli import initialize

    monkeypatch.setenv("DATAAGENT_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("DATAAGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize()
    with pytest.raises(project_context.ProjectNotFound):
        project_context.resolve_project_id("warehouse-a")


def test_default_project_derives_the_existing_storage_names():
    project = project_context.from_values(
        {
            "knowledge_generation": "demo",
            "source_directory": "/tmp/source",
            "generation_retention": "4",
            "schedule_interval_seconds": "30",
            "dimension_value_limit": "2000",
        }
    )
    assert project.project_id == "default"
    assert project.generation == "demo"
    assert project.collection_name() == "sda_demo"
    assert project.index_name("knowledge") == "sda_knowledge"
    assert project.label("Table") == "SDATable"
    assert project.generation_retention == 4
    assert project.schedule_interval_seconds == 30
    assert project.dimension_value_limit_int == 2000
    assert str(project.source_directory) == "/tmp/source"


def test_normalization_keeps_old_defaults():
    values = project_context.normalize_values({})
    assert values["knowledge_generation"] == "default"
    assert values["embedding_model"] == "model/bge-m3"


def test_new_generation_keeps_the_project_prefix():
    project = project_context.from_values(
        {"knowledge_generation": "abc_initial", "_project_id": "abc"}
    )
    generation = project.new_generation()
    assert generation.startswith("abc_")
    assert project.collection_name(generation) == "sda_" + generation
