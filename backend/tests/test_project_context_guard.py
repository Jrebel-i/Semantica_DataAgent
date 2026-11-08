"""Guard the single project-context entry point for project-scoped reads."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "semantica_dataagent"
ENTRY = SOURCE_ROOT / "project_context.py"
MIGRATIONS = SOURCE_ROOT / "migrations"

PROJECT_SETTING_KEYS = {
    "knowledge_generation",
    "source_directory",
    "generation_retention",
    "schedule_interval_seconds",
    "dimension_value_limit",
}
STORAGE_PREFIX = "sda_"
BUILD_QUEUE_NAMES = {"enqueue_build", "add_queued_build"}


def _allowed(path: Path) -> bool:
    return path == ENTRY or MIGRATIONS in path.parents


def _string_values(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            item.value
            for item in node.values
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    return []


def _violations(path: Path, source: str) -> list[str]:
    tree = ast.parse(source, filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        for value in _string_values(node):
            if value in PROJECT_SETTING_KEYS:
                found.append(
                    f"{path.name}:{node.lineno}: direct project setting {value!r}"
                )
            if STORAGE_PREFIX in value:
                found.append(
                    f"{path.name}:{node.lineno}: direct storage prefix {value!r}"
                )
        if isinstance(node, ast.Name) and node.id == "PluginConfig":
            found.append(f"{path.name}:{node.lineno}: direct PluginConfig access")
        if isinstance(node, ast.Attribute) and node.attr == "PluginConfig":
            found.append(f"{path.name}:{node.lineno}: direct PluginConfig access")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "PluginConfig":
                    found.append(
                        f"{path.name}:{node.lineno}: direct PluginConfig import"
                    )
        if isinstance(node, ast.ImportFrom) and node.module in {
            "build_queue",
            ".build_queue",
        }:
            for alias in node.names:
                if alias.name in BUILD_QUEUE_NAMES:
                    found.append(
                        f"{path.name}:{node.lineno}: direct build queue read "
                        f"{alias.name}"
                    )
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "enqueue"
            and isinstance(node.value, ast.Name)
            and node.value.id == "build_queue"
        ):
            found.append(f"{path.name}:{node.lineno}: direct build_queue.enqueue")
    return found


def scan(root: Path = SOURCE_ROOT) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if _allowed(path):
            continue
        found.extend(_violations(path, path.read_text(encoding="utf-8")))
    return found


def test_project_level_reads_only_use_the_context_entry():
    violations = scan()
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        'value = values["knowledge_generation"]\n',
        'value = values["source_directory"]\n',
        'collection = "sda_" + generation\n',
        'path = Path("sda_knowledge")\n',
        "row = db.query(PluginConfig).all()\n",
        "from .build_queue import enqueue_build\n",
        "build_queue.enqueue()\n",
    ],
)
def test_guard_detects_new_direct_reads(tmp_path: Path, source: str):
    path = tmp_path / "violation.py"
    path.write_text(source, encoding="utf-8")
    assert _violations(path, source)
