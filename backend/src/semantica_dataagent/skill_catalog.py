"""Validation and lookup for untrusted Skill packages.

Packages are stored as opaque bytes in SQLite, never executed or unpacked on
installation. Callers must revalidate project access before using a package.
"""

import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass

import yaml
from fastapi import HTTPException

from .db import CatalogSkill, CatalogSkillProject
from .project_api import require_query_project_access
from .skill import SKILLS, read_skill, skill_description, skill_name
from .source_directory import unpack_archive

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SECRET = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_SKILL_BYTES = 64 * 1024
TRUST_WARNING = "上传的 Skill 及其调用的工具可能接触调用时提供的密钥，请仅启用和使用可信任的 Skill。"


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Invalid or duplicate metadata key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def builtins():
    return [
        {
            "name": skill_name(project_type),
            "description": skill_description(project_type),
            "source": "builtin",
            "protected": True,
            "enabled": True,
            "project_types": [project_type],
            "projects": [],
            "secrets": [],
            "dependencies": [],
            "trust_warning": None,
        }
        for project_type in SKILLS
    ]


def validate_package(raw: bytes) -> dict:
    files = unpack_archive(raw)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            seen = set()
            file_paths = {path.casefold() for path in files}
            entry_roots = set()
            for entry in archive.infolist():
                original = entry.orig_filename
                path = original.rstrip("/") if entry.is_dir() else original
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (
                    original != entry.filename
                    or path.casefold() in seen
                    or entry.flag_bits & 1
                    or any(ord(c) < 32 or ord(c) == 127 or c == ":" for c in path)
                    or (entry.is_dir() and mode not in (0, stat.S_IFDIR))
                    or (not entry.is_dir() and mode not in (0, stat.S_IFREG))
                ):
                    raise ValueError("Invalid archive entry")
                seen.add(path.casefold())
                parts = path.split("/")
                entry_roots.add(parts[0])
                if any(part.startswith(".") or len(part) > 255 for part in parts):
                    raise ValueError("Invalid archive path")
                if any(
                    "/".join(parts[:i]).casefold() in file_paths
                    for i in range(1, len(parts))
                ):
                    raise ValueError("File conflicts with directory")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Invalid archive") from exc
    paths = [path.split("/") for path in files]
    if not paths or any(len(path) < 2 for path in paths):
        raise ValueError("Package must contain one directory")
    roots = {path[0] for path in paths}
    if len(roots) != 1:
        raise ValueError("Package must contain one directory")
    root = next(iter(roots))
    if entry_roots != {root}:
        raise ValueError("Package must contain one directory")
    if not NAME.fullmatch(root) or any(
        any(part.startswith(".") or len(part) > 255 for part in path) for path in paths
    ):
        raise ValueError("Invalid package path")
    path = root + "/SKILL.md"
    content = files.get(path)
    if content is None or len(content) > MAX_SKILL_BYTES:
        raise ValueError("Missing or oversized SKILL.md")
    try:
        text = content.decode("utf-8")
        match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
        if match is None:
            raise ValueError("Missing Skill metadata")
        meta = yaml.load(match.group(1), Loader=_UniqueLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Invalid Skill metadata") from exc
    if not isinstance(meta, dict) or meta.get("name") != root:
        raise ValueError("Invalid Skill name")
    description = meta.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 1024
    ):
        raise ValueError("Invalid Skill description")
    types = meta.get("project_types", ["warehouse", "document"])
    projects = meta.get("projects", [])
    secrets = meta.get("secrets", [])
    dependencies = meta.get("dependencies", [])
    if (
        not isinstance(types, list)
        or not types
        or any(t not in ("warehouse", "document") for t in types)
    ):
        raise ValueError("Invalid project types")
    if not isinstance(projects, list) or any(
        not isinstance(p, str) or not p or len(p) > 200 for p in projects
    ):
        raise ValueError("Invalid projects")
    if not isinstance(secrets, list) or any(
        not isinstance(s, str) or not SECRET.fullmatch(s) for s in secrets
    ):
        raise ValueError("Invalid secrets")
    if not isinstance(dependencies, list) or any(
        not isinstance(d, str) or not d or len(d) > 200 for d in dependencies
    ):
        raise ValueError("Invalid dependencies")
    for values in (types, projects, secrets, dependencies):
        if len(values) > 100 or len(values) != len(set(values)):
            raise ValueError("Invalid metadata list")
    return {
        "name": root,
        "description": description.strip(),
        "project_types": types,
        "projects": projects,
        "secrets": secrets,
        "dependencies": dependencies,
        "trust_warning": TRUST_WARNING,
    }


def view(row):
    return {
        **json.loads(row.metadata_json),
        "source": "upload",
        "protected": False,
        "enabled": row.enabled,
    }


@dataclass(frozen=True)
class SkillSnapshot:
    name: str
    source: str
    secrets: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    builtin_content: str | None


def resolve_skill(db, requester, project_id, name):
    """Recheck access at invocation and return an immutable run snapshot.

    Call within the transaction that authorizes a new invocation; do not cache
    this result for later requests. Uploaded archives remain untrusted input.
    """
    if requester.project_id is not None and requester.project_id != project_id:
        raise HTTPException(404, "Skill not available")
    project = require_query_project_access(
        db, project_id, requester, not_found_detail="Skill not available"
    )
    for item in builtins():
        if item["name"] == name and project.type in item["project_types"]:
            return SkillSnapshot(name, "builtin", (), (), read_skill(project.type))
    row = db.get(CatalogSkill, name)
    if row is None or not row.enabled:
        raise HTTPException(404, "Skill not available")
    try:
        metadata = json.loads(row.metadata_json)
        package = bytes(row.archive)
        if validate_package(package) != metadata:
            raise ValueError("Stored metadata differs from package")
        files = tuple(sorted(unpack_archive(package).items()))
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, "Skill package unavailable") from exc
    if project.type not in metadata["project_types"] or (
        metadata["projects"] and db.get(CatalogSkillProject, (name, project_id)) is None
    ):
        raise HTTPException(404, "Skill not available")
    return SkillSnapshot(name, "upload", tuple(metadata["secrets"]), files, None)
