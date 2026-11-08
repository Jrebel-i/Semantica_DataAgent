"""The #173 entry point keeps default-project behavior unchanged."""

from __future__ import annotations

from types import SimpleNamespace

from semantica_dataagent import project_context


def test_resolution_accepts_explicit_and_context_shapes():
    assert project_context.resolve_project_id("warehouse-a") == "default"
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
