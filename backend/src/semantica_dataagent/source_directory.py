"""Git-backed source uploads and read-only history."""

import base64
import binascii
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 1000
MAX_PREVIEW_PATHS = 200
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def relative(value):
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..", ".git") for x in value.split("/")):
        raise ValueError("Invalid path")
    return p


def safe_name(value):
    """Validate a user-supplied category or subfolder path segment."""
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid path")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError("Invalid path")
    path = relative(value)
    if any(part.startswith(".") for part in path.parts):
        raise ValueError("Hidden directories are not allowed")
    return path


def category_path(value):
    path = safe_name(value)
    if len(path.parts) != 1:
        raise ValueError("Knowledge category must be a single directory name")
    return path


def subfolder_path(value):
    return safe_name(value)


def decode(value):
    if len(value) > (MAX_BYTES + 2) // 3 * 4:
        raise ValueError("Upload too large")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Invalid base64") from None


def unpack_archive(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("Upload too large")
    result = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                raise ValueError("Too many archive entries")
            for info in entries:
                name = info.filename.rstrip("/") if info.is_dir() else info.filename
                path = relative(name).as_posix()
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                ):
                    raise ValueError("Unsupported archive entry")
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_BYTES or path in result:
                    raise ValueError("Archive too large or duplicate path")
                with archive.open(info) as stream:
                    data = stream.read(MAX_BYTES + 1)
                if len(data) != info.file_size:
                    raise ValueError("Invalid archive entry")
                result[path] = data
    except (
        zipfile.BadZipFile,
        RuntimeError,
        OSError,
        EOFError,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise ValueError("Invalid archive") from exc
    return result


def git(root, *args, author=None):
    env = os.environ.copy()
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_LITERAL_PATHSPECS="1",
    )
    if author is not None:
        env.update(
            GIT_AUTHOR_NAME=author,
            GIT_AUTHOR_EMAIL="source@localhost",
            GIT_COMMITTER_NAME=author,
            GIT_COMMITTER_EMAIL="source@localhost",
        )
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("Source git operation failed")
    return result.stdout


@contextmanager
def repository(directory):
    root = Path(directory).expanduser().resolve()
    if root == PROJECT_ROOT or root.is_relative_to(PROJECT_ROOT):
        raise ValueError("Source directory must be outside the project repository")
    root.mkdir(parents=True, exist_ok=True)
    # Lock the directory inode: no lock files are created outside the repository.
    lock = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not (root / ".git").exists():
                parent = subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if parent.returncode == 0:
                    raise ValueError("Source directory is inside a git repository")
                git(root, "init", "--quiet")
            if not (root / ".git").is_dir() or (root / ".git").is_symlink():
                raise ValueError("Invalid source repository")
            recover_pulls(root)
            yield root
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        os.close(lock)


@contextmanager
def preview_repository(directory):
    """Lock an existing source directory without initializing or repairing it."""
    root = Path(directory).expanduser().resolve()
    if root == PROJECT_ROOT or root.is_relative_to(PROJECT_ROOT):
        raise ValueError("Source directory must be outside the project repository")
    if not root.exists():
        yield root
        return
    if not root.is_dir():
        raise ValueError("Source directory is not a directory")
    lock = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield root
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def safe(root, path):
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not supported")
    return current


def _upload_paths(files):
    if not files or len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
        raise ValueError("Invalid upload size or file count")
    paths = {relative(name): data for name, data in files.items()}
    if len(paths) != len(files):
        raise ValueError("Duplicate path")
    for path in paths:
        if any(parent in paths for parent in path.parents):
            raise ValueError("Conflicting paths")
    return paths


def _classify(root, target, paths, delete_target):
    uploaded = {(target / path).as_posix() for path in paths}
    added, overwritten, unchanged = [], [], []
    for path, data in paths.items():
        existing = safe(root, target / path)
        if not existing.exists():
            added.append((target / path).as_posix())
            continue
        if not existing.is_file() or existing.is_symlink():
            raise ValueError("Conflicting path in source directory")
        try:
            current = existing.read_bytes()
        except OSError as exc:
            raise ValueError("Cannot read source file") from exc
        if hashlib.sha256(current).digest() == hashlib.sha256(data).digest():
            unchanged.append((target / path).as_posix())
        else:
            overwritten.append((target / path).as_posix())
    deleted = []
    if delete_target is not None:
        destination = safe(root, delete_target)
        if destination.exists() and not destination.is_dir():
            raise ValueError("Target is not a directory")
        if destination.exists():
            for name in list_files(destination):
                path = (delete_target / name).as_posix()
                if path not in uploaded:
                    deleted.append(path)
    return sorted(added), sorted(overwritten), sorted(unchanged), sorted(deleted)


def preview(
    root,
    target,
    files,
    replace,
    category_root="knowledge",
    delete_root=None,
):
    """Compare an upload with the source directory without modifying anything."""
    target = relative(target)
    delete_target = relative(delete_root) if delete_root else target
    if replace and delete_target.parent != relative(category_root):
        raise ValueError("Replacement requires a knowledge category")
    paths = _upload_paths(files)
    for path in paths:
        safe(root, target / path)
    if replace:
        replacement = safe(root, delete_target)
        if replacement.exists():
            if not replacement.is_dir():
                raise ValueError("Target is not a directory")
            if any(path.is_symlink() for path in replacement.rglob("*")):
                raise ValueError("Unsafe replacement directory")
    added, overwritten, unchanged, deleted = _classify(
        root,
        target,
        paths,
        delete_target if replace else None,
    )
    result = {
        "added": added,
        "overwritten": overwritten,
        "unchanged": unchanged,
        "deleted": deleted,
        "truncated": any(
            len(items) > MAX_PREVIEW_PATHS
            for items in (added, overwritten, unchanged, deleted)
        ),
    }
    result["counts"] = {
        "added": len(added),
        "overwritten": len(overwritten),
        "unchanged": len(unchanged),
        "deleted": len(deleted),
    }
    for key in ("added", "overwritten", "unchanged", "deleted"):
        result[key] = result[key][:MAX_PREVIEW_PATHS]
    return result


def upload(
    root,
    target,
    files,
    replace,
    author,
    category_root="knowledge",
    delete_root=None,
):
    target = relative(target)
    delete_target = relative(delete_root) if delete_root else target
    if (
        any(c in author for c in "<>\n\r\x00")
        or not author.strip()
        or author != author.strip()
    ):
        raise ValueError("Token name cannot be represented as a git author")
    if replace and delete_target.parent != relative(category_root):
        raise ValueError("Replacement requires a knowledge category")
    paths = _upload_paths(files)
    for path in paths:
        safe(root, target / path)
    destination = safe(root, target)
    if destination.exists() and not destination.is_dir():
        raise ValueError("Target is not a directory")
    replacement = safe(root, delete_target)
    if replacement.exists() and not replacement.is_dir():
        raise ValueError("Target is not a directory")
    if (
        replace
        and replacement.exists()
        and any(p.is_symlink() for p in replacement.rglob("*"))
    ):
        raise ValueError("Unsafe replacement directory")
    added, overwritten, _, deleted = _classify(
        root,
        target,
        paths,
        delete_target if replace else None,
    )
    if git(root, "diff", "--cached", "--name-only"):
        raise ValueError("Source repository has staged changes")
    try:
        git(root, "rev-parse", "--verify", "HEAD")
        has_head = True
    except ValueError:
        has_head = False
    with tempfile.TemporaryDirectory() as scratch:
        backup = Path(scratch) / "backup"
        existed = replacement.exists()
        if existed:
            shutil.copytree(replacement, backup, symlinks=True)
        try:
            if replace and existed:
                shutil.rmtree(replacement)
            for path, data in paths.items():
                dest = safe(root, target / path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as temp:
                    try:
                        temp.write(data)
                        temp.close()
                        os.replace(temp.name, dest)
                    finally:
                        Path(temp.name).unlink(missing_ok=True)
            staged_paths = (
                (
                    [delete_target.as_posix()]
                    if replace
                    else [(target / path).as_posix() for path in paths]
                )
                if has_head
                else ["."]
            )
            git(root, "add", "-f", "-A", "--", *staged_paths)
            git(
                root,
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                _commit_message(
                    target,
                    category_root,
                    replace,
                    len(added),
                    len(overwritten),
                    len(deleted),
                ),
                author=author,
            )
        except Exception:
            if replacement.exists():
                shutil.rmtree(replacement)
            if existed:
                replacement.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(backup, replacement, symlinks=True)
            if has_head:
                git(root, "reset", "--quiet", "HEAD", "--", delete_target.as_posix())
            else:
                git(root, "rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", ".")
            raise
    return git(root, "rev-parse", "HEAD").decode().strip()


def _commit_message(target, category_root, replace, added, overwritten, deleted):
    try:
        suffix = target.relative_to(relative(category_root))
        category = suffix.parts[0] if suffix.parts else target.name
    except ValueError:
        category = target.name
    message = f"上传知识类 {category}：新增 {added} 个，覆盖 {overwritten} 个"
    if replace:
        message += f"，删除 {deleted} 个"
    return message


def list_files(root):
    def raise_error(error):
        raise error

    paths = []
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=raise_error):
        current = Path(directory)
        dirs[:] = [
            name
            for name in dirs
            if name != ".git" and not (current / name).is_symlink()
        ]
        for name in files:
            path = current / name
            if not path.is_symlink():
                paths.append(path.relative_to(root).as_posix())
    return sorted(paths)


def history(root, path):
    path = relative(path).as_posix()
    try:
        git(root, "rev-parse", "--verify", "HEAD")
    except ValueError:
        return []
    output = git(root, "log", "--format=%H%x00%an%x00%aI%x00%s", "--", path).decode()
    result = [
        dict(
            zip(("commit", "author", "date", "message"), line.split("\0"), strict=True)
        )
        for line in output.splitlines()
    ]
    for row in result:
        row["date"] = datetime.fromisoformat(row["date"]).astimezone(UTC).isoformat()
    return result


def diff(root, path, before, after):
    if not all(
        re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", rev) for rev in (before, after)
    ):
        raise ValueError("Invalid revision")
    return git(
        root,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        before,
        after,
        "--",
        relative(path).as_posix(),
    ).decode("utf-8", errors="replace")


def _last_commit_author(root, rel_path):
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(root),
            "log",
            "-1",
            "--format=%an",
            "--",
            rel_path,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    author = result.stdout.decode("utf-8", errors="replace").strip()
    return author or None


def _atomic_write_bytes(target, data, *, temp_prefix):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=temp_prefix,
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _recover_ossie_pull(root, record, journal):
    version = record["version"]
    backup_dir = root / ".git" / "dataagent-pull-backup"
    paths = record.get("paths") or []
    existed = record.get("existed") or {}
    backups = record.get("backups") or {}
    try:
        head = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
        subject = git(root, "log", "-1", "--format=%s").decode().strip()
    except ValueError:
        head, subject = None, None
    if subject == f"定时拉取 {version}":
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        journal.unlink()
        return
    if head != record["head"]:
        raise ValueError("Source HEAD changed during pull recovery")
    staged = [
        item
        for item in git(root, "diff", "--cached", "--name-only", "-z")
        .decode()
        .split("\0")
        if item
    ]
    allowed = set(paths)
    if any(item not in allowed for item in staged):
        raise ValueError("Source repository has staged changes during pull recovery")
    if paths:
        if head:
            git(root, "reset", "--quiet", "HEAD", "--", *paths)
        else:
            git(root, "rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", *paths)

    # Remove any partial temp files created before crash
    for p_rel in paths:
        target = safe(root, relative(p_rel))
        if target.parent.is_dir():
            for leftover in target.parent.glob(f".pull-{version}-*"):
                leftover.unlink(missing_ok=True)

    for p_rel in paths:
        target = safe(root, relative(p_rel))
        if existed.get(p_rel):
            bak_name = backups.get(p_rel)
            if bak_name:
                bak_file = backup_dir / bak_name
                if bak_file.is_file():
                    _atomic_write_bytes(
                        target,
                        bak_file.read_bytes(),
                        temp_prefix=f".recover-{version}-",
                    )
        else:
            target.unlink(missing_ok=True)
            parent = target.parent
            while parent != root and parent.is_dir():
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent

    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    journal.unlink()


def recover_pulls(root):
    """Recover a pull interrupted before or after its git commit."""
    journal = root / ".git" / "dataagent-pull.json"
    if not journal.exists():
        return
    record = json.loads(journal.read_text(encoding="utf-8"))
    version = record["version"]
    if not re.fullmatch(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}", version):
        raise ValueError("Invalid pull recovery journal")
    if record.get("mode") == "ossie":
        _recover_ossie_pull(root, record, journal)
        return
    path = f"dtb/{version}"
    try:
        head = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
        subject = git(root, "log", "-1", "--format=%s").decode().strip()
    except ValueError:
        head, subject = None, None
    if subject == f"定时拉取 {version}":
        journal.unlink()
        return
    if head != record["head"]:
        raise ValueError("Source HEAD changed during pull recovery")
    staged = git(root, "diff", "--cached", "--name-only", "-z").decode().split("\0")
    if any(item and not item.startswith(path + "/") for item in staged):
        raise ValueError("Source repository has staged changes during pull recovery")
    if head:
        git(root, "reset", "--quiet", "HEAD", "--", path)
    else:
        git(root, "rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", path)
    for item in (root / "dtb" / version, root / "dtb" / (".pull-" + version)):
        if item.exists():
            shutil.rmtree(item)
    journal.unlink()


def publish_ossie_pull(root, files, *, ossie_path="ossie", operation_id=None):
    """Publish DTB-derived Ossie outputs atomically while protecting manual files."""
    import uuid

    recover_pulls(root)
    if git(root, "diff", "--cached", "--name-only"):
        raise ValueError("Source repository has staged changes")
    if not files:
        raise ValueError("Empty pull snapshot")
    rel_ossie = safe_name(ossie_path).as_posix()
    paths = {relative(name): content for name, content in files.items()}
    if len(paths) != len(files):
        raise ValueError("Duplicate pull snapshot path")

    new_paths_str: set[str] = set()
    for rel_p in paths:
        p_str = rel_p.as_posix()
        safe(root, rel_p)
        if not (p_str == "dimension_values.json" or p_str.startswith(rel_ossie + "/")):
            raise ValueError(f"Unexpected Ossie pull path: {p_str}")
        new_paths_str.add(p_str)

    if operation_id is not None and not re.fullmatch(
        r"[A-Za-z0-9_-]{1,80}", operation_id
    ):
        raise ValueError("Invalid pull operation identifier")

    stale_delete: list[str] = []
    ossie_dir = safe(root, relative(rel_ossie))
    if ossie_dir.is_dir():
        for existing in sorted(ossie_dir.rglob("*")):
            if not existing.is_file():
                continue
            p_str = existing.relative_to(root).as_posix()
            if (
                existing.suffix.lower() in {".yaml", ".yml", ".json"}
                and p_str not in new_paths_str
                and _last_commit_author(root, p_str) == "定时拉取"
            ):
                stale_delete.append(p_str)

    dim_file = safe(root, relative("dimension_values.json"))
    if (
        dim_file.is_file()
        and "dimension_values.json" not in new_paths_str
        and _last_commit_author(root, "dimension_values.json") == "定时拉取"
    ):
        stale_delete.append("dimension_values.json")

    all_paths = sorted(new_paths_str | set(stale_delete))
    try:
        head = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    except ValueError:
        head = None

    version = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex
    backup_dir = root / ".git" / "dataagent-pull-backup"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    existed: dict[str, bool] = {}
    backups: dict[str, str] = {}
    for idx, p_str in enumerate(all_paths):
        target = safe(root, relative(p_str))
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise ValueError(f"Conflicting path in source directory: {p_str}")
            bak_name = f"{idx}.bak"
            _atomic_write_bytes(
                backup_dir / bak_name,
                target.read_bytes(),
                temp_prefix=f".backup-{version}-",
            )
            existed[p_str] = True
            backups[p_str] = bak_name
        else:
            existed[p_str] = False

    journal = root / ".git" / "dataagent-pull.json"
    with tempfile.NamedTemporaryFile(
        mode="w", dir=journal.parent, prefix="dataagent-pull-journal-", delete=False
    ) as temp:
        try:
            json.dump(
                {
                    "mode": "ossie",
                    "version": version,
                    "head": head,
                    "paths": all_paths,
                    "existed": existed,
                    "backups": backups,
                },
                temp,
            )
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(temp.name, journal)
        finally:
            Path(temp.name).unlink(missing_ok=True)

    try:
        for rel_p, content in paths.items():
            target = safe(root, rel_p)
            _atomic_write_bytes(target, content, temp_prefix=f".pull-{version}-")
        for p_str in stale_delete:
            safe(root, relative(p_str)).unlink(missing_ok=True)
        git(root, "add", "-A", "--", *all_paths)
        message = ["-m", f"Pull-Operation: {operation_id}"] if operation_id else []
        git(
            root,
            "commit",
            "--allow-empty",
            "--quiet",
            "-m",
            f"定时拉取 {version}",
            *message,
            author="定时拉取",
        )
        commit = git(root, "rev-parse", "HEAD").decode().strip()
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        journal.unlink()
        return commit
    except BaseException:
        recover_pulls(root)
        raise


def publish_pull(root, files, operation_id=None):
    """Publish an immutable DTB snapshot while holding the source lock."""
    import uuid

    recover_pulls(root)
    if git(root, "diff", "--cached", "--name-only"):
        raise ValueError("Source repository has staged changes")
    if not files:
        raise ValueError("Empty pull snapshot")
    paths = {relative(name): content for name, content in files.items()}
    if len(paths) != len(files):
        raise ValueError("Duplicate pull snapshot path")
    parent = safe(root, relative("dtb"))
    try:
        head = git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    except ValueError:
        head = None
    version = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex
    destination = parent / version
    staging = parent / (".pull-" + version)
    if operation_id is not None and not re.fullmatch(
        r"[A-Za-z0-9_-]{1,80}", operation_id
    ):
        raise ValueError("Invalid pull operation identifier")
    journal = root / ".git" / "dataagent-pull.json"
    with tempfile.NamedTemporaryFile(
        mode="w", dir=journal.parent, delete=False
    ) as temp:
        try:
            json.dump({"version": version, "head": head}, temp)
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(temp.name, journal)
        finally:
            Path(temp.name).unlink(missing_ok=True)
    try:
        parent.mkdir(exist_ok=True)
        staging.mkdir()
        (staging / "pull-manifest.json").write_text(
            json.dumps({"published_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )
        for path, content in paths.items():
            target = staging.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        os.replace(staging, destination)
        git(root, "add", "-f", "--", f"dtb/{version}")
        message = ["-m", f"Pull-Operation: {operation_id}"] if operation_id else []
        git(
            root,
            "commit",
            "--quiet",
            "-m",
            f"定时拉取 {version}",
            *message,
            author="定时拉取",
        )
        commit = git(root, "rev-parse", "HEAD").decode().strip()
        journal.unlink()
        return commit
    except BaseException:
        recover_pulls(root)
        raise
