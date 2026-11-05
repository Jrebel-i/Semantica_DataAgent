"""The retrieval skill shared by the MCP prompt and REST download."""

import re
from importlib.resources import files
from pathlib import Path

SKILL_FILENAME = "dataagent-retrieval-SKILL.md"
_PACKAGE_PATH = "skills/dataagent-retrieval/SKILL.md"
_SOURCE_PATH = Path(__file__).resolve().parents[3] / _PACKAGE_PATH


def read_skill() -> str:
    if _SOURCE_PATH.is_file():
        return _SOURCE_PATH.read_text(encoding="utf-8")
    packaged = files("semantica_dataagent").joinpath(_PACKAGE_PATH)
    if not packaged.is_file():
        raise FileNotFoundError("Retrieval skill is missing from the installation")
    return packaged.read_text(encoding="utf-8")


def _frontmatter(key: str) -> str:
    text = read_skill()
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    if match is None:
        raise ValueError("Retrieval skill has no frontmatter")
    for line in match.group(1).splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == key and value.strip():
            return value.strip()
    raise ValueError(f"Retrieval skill has no {key}")


SKILL_NAME = _frontmatter("name")
SKILL_DESCRIPTION = _frontmatter("description")


def register_prompt(server) -> None:
    server.prompt(
        name=SKILL_NAME,
        title="Semantica DataAgent retrieval",
        description=SKILL_DESCRIPTION,
    )(read_skill)
