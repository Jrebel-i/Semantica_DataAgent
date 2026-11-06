"""Git-backed source uploads and read-only history."""

import base64
import binascii
import fcntl
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
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def relative(value):
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..", ".git") for x in value.split("/")):
        raise ValueError("Invalid path")
    return p


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


def safe(root, path):
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not supported")
    return current


def upload(root, target, files, replace, author, category_root="knowledge"):
    target = relative(target)
    if (
        any(c in author for c in "<>\n\r\x00")
        or not author.strip()
        or author != author.strip()
    ):
        raise ValueError("Token name cannot be represented as a git author")
    if replace and target.parent != relative(category_root):
        raise ValueError("Replacement requires a knowledge category")
    if not files or len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
        raise ValueError("Invalid upload size or file count")
    paths = {relative(name): data for name, data in files.items()}
    if len(paths) != len(files):
        raise ValueError("Duplicate path")
    for path in paths:
        if any(parent in paths for parent in path.parents):
            raise ValueError("Conflicting paths")
        safe(root, target / path)
    destination = safe(root, target)
    if destination.exists() and not destination.is_dir():
        raise ValueError("Target is not a directory")
    if (
        replace
        and destination.exists()
        and any(p.is_symlink() for p in destination.rglob("*"))
    ):
        raise ValueError("Unsafe replacement directory")
    if git(root, "diff", "--cached", "--name-only"):
        raise ValueError("Source repository has staged changes")
    try:
        git(root, "rev-parse", "--verify", "HEAD")
        has_head = True
    except ValueError:
        has_head = False
    with tempfile.TemporaryDirectory() as scratch:
        backup = Path(scratch) / "backup"
        existed = destination.exists()
        if existed:
            shutil.copytree(destination, backup, symlinks=True)
        try:
            if replace and existed:
                shutil.rmtree(destination)
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
                    [target.as_posix()]
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
                "Upload sources",
                author=author,
            )
        except Exception:
            if destination.exists():
                shutil.rmtree(destination)
            if existed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(backup, destination, symlinks=True)
            if has_head:
                git(root, "reset", "--quiet", "HEAD", "--", target.as_posix())
            else:
                git(root, "rm", "-r", "-f", "--cached", "--ignore-unmatch", "--", ".")
            raise
    return git(root, "rev-parse", "HEAD").decode().strip()


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


def recover_pulls(root):
    """Recover a pull interrupted before or after its git commit."""
    journal = root / ".git" / "dataagent-pull.json"
    if not journal.exists():
        return
    record = json.loads(journal.read_text(encoding="utf-8"))
    version = record["version"]
    if not re.fullmatch(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}", version):
        raise ValueError("Invalid pull recovery journal")
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
