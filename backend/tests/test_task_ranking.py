"""Offline checks for task retrieval ranking."""

from semantica_dataagent.task_stage import rank_task_hits


def test_tasks_with_output_rank_first_and_preserve_relevance_within_groups():
    hits = [
        {"ref": "task:no-1", "has_output": False},
        {"ref": "task:yes-1", "has_output": True},
        {"ref": "task:no-2", "has_output": False},
        {"ref": "task:yes-2", "has_output": True},
        {"ref": "task:no-3"},
    ]
    assert [hit["ref"] for hit in rank_task_hits(hits)] == [
        "task:yes-1",
        "task:yes-2",
        "task:no-1",
        "task:no-2",
        "task:no-3",
    ]
    assert [hit["ref"] for hit in hits] == [
        "task:no-1",
        "task:yes-1",
        "task:no-2",
        "task:yes-2",
        "task:no-3",
    ]
