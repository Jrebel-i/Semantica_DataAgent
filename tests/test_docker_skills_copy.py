"""Guard that every force-included build input is copied into the app image."""

import posixpath
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "k8s" / "app" / "Dockerfile"
PYPROJECT = ROOT / "backend" / "pyproject.toml"


def dockerfile_copy_sources(text: str) -> list[str]:
    sources: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        arguments = shlex.split(stripped)[1:]
        arguments = [
            argument for argument in arguments if not argument.startswith("--")
        ]
        if len(arguments) < 2:
            continue
        sources.extend(arguments[:-1])
    return [posixpath.normpath(source) for source in sources]


def covered_by_copy(build_path: str, copy_source: str) -> bool:
    if copy_source == ".":
        return True
    return build_path == copy_source or build_path.startswith(copy_source + "/")


def force_included_build_paths() -> list[str]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]
    return [
        path
        for entry in force_include
        if entry.startswith("../")
        for path in [posixpath.normpath(posixpath.join("backend", entry))]
    ]


def test_force_included_sources_are_copied_into_image() -> None:
    copy_sources = dockerfile_copy_sources(DOCKERFILE.read_text(encoding="utf-8"))
    missing = [
        path
        for path in force_included_build_paths()
        if not any(covered_by_copy(path, source) for source in copy_sources)
    ]
    assert not missing, (
        "deploy/k8s/app/Dockerfile 没有 COPY 以下 force-include 源路径: "
        + ", ".join(missing)
    )
