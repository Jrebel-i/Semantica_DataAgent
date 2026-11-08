"""The retrieval skills shared by the MCP prompt and REST download."""

import re
from importlib.resources import files
from pathlib import Path

from .project_types import DOCUMENT, WAREHOUSE

SKILLS = {
    WAREHOUSE: {
        "filename": "dataagent-retrieval-SKILL.md",
        "package_path": "skills/dataagent-retrieval/SKILL.md",
    },
    DOCUMENT: {
        "filename": "dataagent-document-retrieval-SKILL.md",
        "package_path": "skills/dataagent-document-retrieval/SKILL.md",
    },
}
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _spec(project_type: str) -> dict[str, str]:
    try:
        return SKILLS[project_type]
    except KeyError as exc:
        raise ValueError(f"未知项目类型：{project_type}") from exc


def _source_path(project_type: str) -> Path:
    return _REPO_ROOT / _spec(project_type)["package_path"]


def read_skill(project_type: str = WAREHOUSE) -> str:
    spec = _spec(project_type)
    source = _source_path(project_type)
    if source.is_file():
        return source.read_text(encoding="utf-8")
    packaged = files("semantica_dataagent").joinpath(spec["package_path"])
    if not packaged.is_file():
        raise FileNotFoundError("Retrieval skill is missing from the installation")
    return packaged.read_text(encoding="utf-8")


def skill_filename(project_type: str = WAREHOUSE) -> str:
    return _spec(project_type)["filename"]


def _frontmatter(project_type: str, key: str) -> str:
    text = read_skill(project_type)
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    if match is None:
        raise ValueError("Retrieval skill has no frontmatter")
    for line in match.group(1).splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == key and value.strip():
            return value.strip()
    raise ValueError(f"Retrieval skill has no {key}")


def skill_name(project_type: str = WAREHOUSE) -> str:
    return _frontmatter(project_type, "name")


def skill_description(project_type: str = WAREHOUSE) -> str:
    return _frontmatter(project_type, "description")


SKILL_FILENAME = skill_filename(WAREHOUSE)
SKILL_NAME = skill_name(WAREHOUSE)
SKILL_DESCRIPTION = skill_description(WAREHOUSE)


def register_prompt(server) -> None:
    def retrieval_skill() -> str:
        return read_skill(WAREHOUSE)

    server.prompt(
        name=SKILL_NAME,
        title="Semantica DataAgent retrieval",
        description=SKILL_DESCRIPTION,
    )(retrieval_skill)
