"""Validation and lookup for untrusted Skill packages.

Packages are stored as opaque bytes in SQLite, never executed or unpacked on
installation. Callers must revalidate project access before using a package.
"""

import io
import json
import re
import stat
import struct
import zipfile
from dataclasses import dataclass

import yaml
from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.exc import OperationalError

from .db import CatalogSkill, CatalogSkillProject, Token, digest, sessions
from .project_api import active_skill_project, require_skill_project
from .skill import SKILLS, read_skill, skill_description, skill_name
from .source_directory import relative, unpack_archive

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SECRET = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
RESERVED_SECRET_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PWD",
        "OLDPWD",
        "IFS",
        "ENV",
        "BASH_ENV",
        "CDPATH",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "SYSTEMROOT",
        "COMSPEC",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "FTP_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GIT_SSH_COMMAND",
        "GIT_EXEC_PATH",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "EDITOR",
        "VISUAL",
        "PAGER",
    }
)
RESERVED_SECRET_PREFIXES = (
    "DATAAGENT_",
    "ANTHROPIC_",
    "OPENAI_",
    "CLAUDE_",
    "CODEX_",
    "PI_",
    "KUBERNETES_",
    "LD_",
    "DYLD_",
    "NODE_",
    "PYTHON",
    "LC_",
    "BASH_FUNC_",
    "SSL_",
    "TLS_",
    "CURL_",
    "GIT_",
)
MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_SKILL_BYTES = 64 * 1024
TRUST_WARNING = "上传的 Skill 及其调用的工具可能接触调用时提供的密钥，请仅启用和使用可信任的 Skill。"
SAFE_PACKAGE_ERROR_MESSAGES: dict[str, str] = {
    msg: msg
    for msg in (
        "Upload too large",
        "Missing ZIP directory",
        "Ambiguous ZIP directory",
        "Unsupported ZIP directory",
        "Invalid ZIP directory",
        "Unsupported ZIP entry",
        "Invalid ZIP entry length",
        "Invalid ZIP extra field",
        "Unsupported ZIP extra field",
        "Too many archive entries",
        "Invalid ZIP entry count",
        "Directory entry contains data",
        "Invalid directory header",
        "Directory header differs from archive",
        "Invalid directory data descriptor",
        "Invalid archive entry",
        "Unsupported archive entry",
        "Archive path too deep",
        "Invalid archive path",
        "Invalid path",
        "File conflicts with directory",
        "Archive too large or duplicate path",
        "Invalid archive",
        "Package must contain one directory or root SKILL.md",
        "Invalid package path",
        "Missing or oversized SKILL.md",
        "Missing Skill metadata",
        "Invalid Skill metadata",
        "Skill metadata too complex",
        "Invalid or duplicate metadata key",
        "Invalid Skill name",
        "Invalid Skill description",
        "Invalid project types",
        "Invalid projects",
        "Invalid secrets",
        "Invalid dependencies",
        "Invalid metadata list",
        "Invalid metadata text",
    )
}


def format_package_error(exc: ValueError) -> str:
    """Return a bounded, user-safe 422 message for package validation failures."""
    reason = SAFE_PACKAGE_ERROR_MESSAGES.get(str(exc))
    return f"Invalid Skill package: {reason}" if reason else "Invalid Skill package"


def normalize_package_files(
    name: str, files: dict[str, bytes]
) -> tuple[tuple[str, bytes], ...]:
    """Return sorted `(relative_path, content)` tuples always nested under `<name>/`."""
    if "SKILL.md" in files:
        return tuple(sorted((f"{name}/{path}", data) for path, data in files.items()))
    return tuple(sorted(files.items()))


def is_reserved_secret_name(name: str) -> bool:
    """Return True if a secret declaration could shadow system or runner env vars."""
    return name in RESERVED_SECRET_NAMES or name.startswith(RESERVED_SECRET_PREFIXES)


class _UniqueLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0
        self.nodes = 0

    def compose_node(self, parent, index):
        self.depth += 1
        self.nodes += 1
        try:
            if self.depth > 20 or self.nodes > 5000:
                raise ValueError("Skill metadata too complex")
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Invalid or duplicate metadata key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _preflight_zip(raw: bytes) -> None:
    """Bound central-directory work before ZipFile allocates ZipInfo objects."""
    start = max(0, len(raw) - 65557)
    end = raw.rfind(b"PK\x05\x06", start)
    if end < 0 or end + 22 > len(raw):
        raise ValueError("Missing ZIP directory")
    record = struct.unpack_from("<4s4H2IH", raw, end)
    if end + 22 + record[7] != len(raw) or (
        end >= 20 and raw[end - 20 : end - 16] == b"PK\x06\x07"
    ):
        raise ValueError("Ambiguous ZIP directory")
    _, disk, directory_disk, disk_count, count, size, offset, _ = record
    if (
        disk
        or directory_disk
        or disk_count != count
        or count > 1000
        or size > 2 * 1024 * 1024
        or offset + size != end
    ):
        raise ValueError("Unsupported ZIP directory")
    pos = offset
    actual = 0
    while pos < end:
        if pos + 46 > end:
            raise ValueError("Invalid ZIP directory")
        entry = struct.unpack_from("<4s6H3I5H2I", raw, pos)
        if (
            entry[0] != b"PK\x01\x02"
            or entry[8] == 0xFFFFFFFF
            or entry[9] == 0xFFFFFFFF
            or entry[13] != 0
            or entry[16] == 0xFFFFFFFF
        ):
            raise ValueError("Unsupported ZIP entry")
        name_size, extra_size, comment_size = entry[10:13]
        next_pos = pos + 46 + name_size + extra_size + comment_size
        if next_pos > end:
            raise ValueError("Invalid ZIP entry length")
        extra_pos = pos + 46 + name_size
        extra_end = extra_pos + extra_size
        while extra_pos < extra_end:
            if extra_pos + 4 > extra_end:
                raise ValueError("Invalid ZIP extra field")
            kind, length = struct.unpack_from("<HH", raw, extra_pos)
            extra_pos += 4 + length
            if kind == 1 or extra_pos > extra_end:
                raise ValueError("Unsupported ZIP extra field")
        actual += 1
        if actual > 1000:
            raise ValueError("Too many archive entries")
        pos = next_pos
    if actual != count:
        raise ValueError("Invalid ZIP entry count")


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


def _validate_directory_entry(raw: bytes, archive, entry) -> None:
    if entry.file_size != 0:
        raise ValueError("Directory entry contains data")
    if entry.header_offset + 30 > len(raw):
        raise ValueError("Invalid directory header")
    header = struct.unpack_from("<4s5H3I2H", raw, entry.header_offset)
    (
        _,
        _,
        flags,
        method,
        _,
        _,
        crc,
        compressed,
        size,
        name_len,
        extra_len,
    ) = header
    if flags != entry.flag_bits or method != entry.compress_type:
        raise ValueError("Directory header differs from archive")
    expected = (entry.CRC, entry.compress_size, entry.file_size)
    actual = (crc, compressed, size)
    if (
        flags & 8
        and any(a not in (0, b) for a, b in zip(actual, expected, strict=True))
    ) or (not flags & 8 and actual != expected):
        raise ValueError("Directory header differs from archive")
    if flags & 8:
        offset = entry.header_offset + 30 + name_len + extra_len + entry.compress_size
        if raw[offset : offset + 4] == b"PK\x07\x08":
            offset += 4
        if (
            offset + 12 > len(raw)
            or struct.unpack_from("<III", raw, offset) != expected
        ):
            raise ValueError("Invalid directory data descriptor")
    with archive.open(entry) as stream:
        if stream.read(1):
            raise ValueError("Directory entry contains data")


def validate_package(raw: bytes) -> dict:
    if len(raw) > MAX_PACKAGE_BYTES:
        raise ValueError("Upload too large")
    _preflight_zip(raw)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000:
                raise ValueError("Too many archive entries")
            seen = set()
            file_paths = {
                entry.filename.casefold() for entry in entries if not entry.is_dir()
            }
            entry_roots = set()
            entry_paths: list[str] = []
            for entry in entries:
                original = entry.orig_filename
                path = original.rstrip("/") if entry.is_dir() else original
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (
                    original != entry.filename
                    or len(path) > 1024
                    or path.casefold() in seen
                    or entry.flag_bits & 1
                    or any(
                        ord(c) < 32 or 127 <= ord(c) <= 159 or c == ":" for c in path
                    )
                    or (entry.is_dir() and mode not in (0, stat.S_IFDIR))
                    or (not entry.is_dir() and mode not in (0, stat.S_IFREG))
                ):
                    raise ValueError("Invalid archive entry")
                relative(path)
                if entry.is_dir():
                    _validate_directory_entry(raw, archive, entry)
                seen.add(path.casefold())
                entry_paths.append(path)
                parts = path.split("/")
                if len(parts) > 32:
                    raise ValueError("Archive path too deep")
                entry_roots.add(parts[0])
                if any(part.startswith(".") or len(part) > 255 for part in parts):
                    raise ValueError("Invalid archive path")
                if any(
                    "/".join(parts[:i]).casefold() in file_paths
                    for i in range(1, len(parts))
                ):
                    raise ValueError("File conflicts with directory")
    except (
        zipfile.BadZipFile,
        OSError,
        EOFError,
        NotImplementedError,
        struct.error,
        OverflowError,
    ) as exc:
        raise ValueError("Invalid archive") from exc
    files = unpack_archive(raw)
    if not files:
        raise ValueError("Package must contain one directory or root SKILL.md")
    if "SKILL.md" in files:
        content = files["SKILL.md"]
        if len(content) > MAX_SKILL_BYTES:
            raise ValueError("Missing or oversized SKILL.md")
        try:
            text = content.decode("utf-8")
            match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
            if match is None:
                raise ValueError("Missing Skill metadata")
            meta = yaml.load(match.group(1), Loader=_UniqueLoader)
        except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
            raise ValueError("Invalid Skill metadata") from exc
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("name"), str)
            or not NAME.fullmatch(meta["name"])
        ):
            raise ValueError("Invalid Skill name")
        root = meta["name"]
        for entry_path in entry_paths:
            if len(root) + 1 + len(entry_path) > 1024:
                raise ValueError("Invalid archive entry")
            if len(entry_path.split("/")) + 1 > 32:
                raise ValueError("Archive path too deep")
        if any(
            any(part.startswith(".") or len(part) > 255 for part in path.split("/"))
            for path in files
        ):
            raise ValueError("Invalid package path")
    else:
        paths = [path.split("/") for path in files]
        if any(len(path) < 2 for path in paths):
            raise ValueError("Package must contain one directory or root SKILL.md")
        roots = {path[0] for path in paths}
        if len(roots) != 1:
            raise ValueError("Package must contain one directory or root SKILL.md")
        root = next(iter(roots))
        if entry_roots != {root}:
            raise ValueError("Package must contain one directory or root SKILL.md")
        if not NAME.fullmatch(root) or any(
            any(part.startswith(".") or len(part) > 255 for part in path)
            for path in paths
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
        except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
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
        not isinstance(s, str) or not SECRET.fullmatch(s) or is_reserved_secret_name(s)
        for s in secrets
    ):
        raise ValueError("Invalid secrets")
    if not isinstance(dependencies, list) or any(
        not isinstance(d, str) or not d or len(d) > 200 for d in dependencies
    ):
        raise ValueError("Invalid dependencies")
    for values in (types, projects, secrets, dependencies):
        if len(values) > 100 or len(values) != len(set(values)):
            raise ValueError("Invalid metadata list")
    for value in (description, *types, *projects, *secrets, *dependencies):
        value.encode("utf-8", errors="strict")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise ValueError("Invalid metadata text")
    return {
        "name": root,
        "description": description.strip(),
        "project_types": types,
        "projects": projects,
        "secrets": secrets,
        "dependencies": dependencies,
    }


def view(row):
    return {
        **json.loads(row.metadata_json),
        "trust_warning": TRUST_WARNING,
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
    connection = db.connection()
    if connection.connection.driver_connection.in_transaction:
        try:
            connection.execute(text("UPDATE projects SET status = status WHERE 0"))
        except OperationalError as exc:
            raise HTTPException(
                409, "Skill lookup requires a current transaction"
            ) from exc
    record = _authorized_record(db, requester, project_id, name, contents=True)
    return _materialize_skill(name, record)


def _authorized_record(
    db, requester, project_id, name, *, contents, builtin_pairs=None
):
    project_type = require_skill_project(db, requester, project_id)
    if builtin_pairs is None:
        builtin_pairs = tuple(
            (item["name"], project_type)
            for item in builtins()
            for project_type in item["project_types"]
        )
    if (name, project_type) in builtin_pairs:
        return ("builtin", project_type, None, None)
    types = func.json_each(CatalogSkill.metadata_json, "$.project_types").table_valued(
        "value"
    )
    type_allowed = exists(
        select(1).select_from(types).where(types.c.value == project_type)
    )
    project_allowed = exists().where(
        CatalogSkillProject.skill_name == CatalogSkill.name,
        CatalogSkillProject.project_id == project_id,
    )
    columns = (
        (CatalogSkill.generation, CatalogSkill.metadata_json, CatalogSkill.archive)
        if contents
        else (CatalogSkill.generation,)
    )
    row = db.execute(
        select(*columns).where(
            CatalogSkill.name == name,
            CatalogSkill.enabled.is_(True),
            active_skill_project(requester, project_id, project_type),
            type_allowed,
            or_(
                func.json_array_length(
                    func.json_extract(CatalogSkill.metadata_json, "$.projects")
                )
                == 0,
                project_allowed,
            ),
        )
    ).one_or_none()
    if row is None:
        require_skill_project(db, requester, project_id)
        raise HTTPException(404, "Skill not available")
    return (
        "upload",
        row.generation,
        row.metadata_json if contents else None,
        bytes(row.archive) if contents else None,
    )


def _materialize_skill(name, record):
    source, generation, metadata_json, archive = record
    if source == "builtin":
        return SkillSnapshot(name, "builtin", (), (), read_skill(generation))
    try:
        metadata = json.loads(metadata_json)
        package = archive
        if validate_package(package) != metadata:
            raise ValueError("Stored metadata differs from package")
        files = normalize_package_files(name, unpack_archive(package))
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, "Skill package unavailable") from exc
    if metadata["name"] != name:
        raise HTTPException(404, "Skill not available")
    return SkillSnapshot(name, "upload", tuple(metadata["secrets"]), files, None)


def prepare_skill(requester, project_id, name):
    """Read and validate a package without holding a database transaction."""
    builtin_pairs = tuple(
        (item["name"], project_type)
        for item in builtins()
        for project_type in item["project_types"]
    )
    with sessions()() as db:
        require_personal_identity(db, requester)
        record = _authorized_record(
            db, requester, project_id, name, contents=True, builtin_pairs=builtin_pairs
        )
    return (record[:2], builtin_pairs), _materialize_skill(name, record)


def require_prepared_skill(db, requester, project_id, name, version):
    """Recheck current authorization and package generation under the write lock."""
    require_personal_identity(db, requester)
    expected, builtin_pairs = version
    current = _authorized_record(
        db, requester, project_id, name, contents=False, builtin_pairs=builtin_pairs
    )
    if current[:2] != expected:
        raise HTTPException(409, "Skill changed during preparation")


def require_personal_identity(db, requester):
    token = db.get(Token, digest(requester.token))
    if token is None or token.revoked or token.id != requester.id:
        raise HTTPException(404, "Project not found")
