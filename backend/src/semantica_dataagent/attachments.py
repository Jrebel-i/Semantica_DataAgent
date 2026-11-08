"""Attachment limits, safe names and per-conversation workspace storage."""

from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

MAX_ATTACHMENT_FILES = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 50 * 1024 * 1024
# Multipart boundaries and part headers add a little to the raw file bytes.
MAX_REQUEST_BYTES = MAX_ATTACHMENT_TOTAL_BYTES + 1024 * 1024
CHUNK = 1024 * 1024
IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


class AttachmentLimitError(ValueError):
    """The request exceeds one of the documented attachment limits."""


class AttachmentStorageError(RuntimeError):
    """The attachment cannot be stored inside this conversation's workspace."""


@dataclass(slots=True)
class StagedUpload:
    original_name: str
    stored_name: str
    path: Path
    size: int
    mime_type: str
    is_image: bool


class StagedUploads:
    def __init__(self, directory: Path, items: list[StagedUpload]):
        self.directory = directory
        self.items = items

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _display_name(value: str) -> str:
    return value.replace("/", "_").replace("\\", "_").replace("\x00", "_")


def safe_filename(raw: str) -> str:
    """Return a single path component with no traversal or control characters."""
    value = (raw or "").replace("\\", "/")
    value = value.rsplit("/", 1)[-1]
    value = "".join(
        "_"
        if ch in {"/", "\\", "\x00"} or unicodedata.category(ch).startswith("C")
        else ch
        for ch in value
    )
    value = value.strip().strip(".").strip()
    if not value or value in {".", ".."}:
        value = "attachment"
    if len(value.encode("utf-8")) > 180:
        stem, suffix = os.path.splitext(value)
        if len(suffix.encode("utf-8")) > 24:
            stem, suffix = value, ""
        budget = 180 - len(suffix.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
        value = (stem or "attachment") + suffix
    return value


def looks_like_image(filename: str, content_type: str | None) -> bool:
    if content_type and content_type.lower().startswith("image/"):
        return True
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def _mime_type(filename: str, content_type: str | None) -> str:
    if content_type:
        return content_type.split(";", 1)[0].strip().lower()
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _workspace_candidate(root: Path, conversation_id: str) -> Path:
    if (
        not conversation_id
        or conversation_id in {".", ".."}
        or "/" in conversation_id
        or "\\" in conversation_id
        or "\x00" in conversation_id
        or Path(conversation_id).name != conversation_id
    ):
        raise AttachmentStorageError("invalid conversation id")
    return Path(root).resolve() / conversation_id


def workspace_directory(root: Path, conversation_id: str) -> Path:
    root_path = Path(root).resolve()
    candidate = _workspace_candidate(root_path, conversation_id)
    if candidate.is_symlink():
        raise AttachmentStorageError("conversation workspace is a symlink")
    workspace = candidate.resolve()
    if workspace.parent != root_path:
        raise AttachmentStorageError("conversation workspace escapes agent root")
    return workspace


def attachment_directory(root: Path, conversation_id: str) -> Path:
    workspace = workspace_directory(root, conversation_id)
    work = workspace / "work"
    if work.is_symlink():
        raise AttachmentStorageError("conversation work directory is a symlink")
    directory = work / "attachments"
    if directory.is_symlink():
        raise AttachmentStorageError("attachment directory is a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise AttachmentStorageError("attachment directory is a symlink")
    os.chmod(directory, 0o700)
    return directory


def remove_conversation_workspace(root: Path, conversation_id: str) -> None:
    try:
        candidate = _workspace_candidate(root, conversation_id)
    except AttachmentStorageError:
        return
    if candidate.is_symlink():
        candidate.unlink(missing_ok=True)
        return
    if candidate.is_dir():
        shutil.rmtree(candidate)
    elif candidate.exists():
        candidate.unlink()


def _temp_directory() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="sda-attachments-"))
    os.chmod(directory, 0o700)
    return directory


async def stage_uploads(uploads: list[UploadFile]) -> StagedUploads:
    if len(uploads) > MAX_ATTACHMENT_FILES:
        raise AttachmentLimitError("一次最多添加 10 个附件")
    directory = _temp_directory()
    items: list[StagedUpload] = []
    total = 0
    try:
        for upload in uploads:
            original = upload.filename or "attachment"
            name = safe_filename(original)
            target = directory / name
            index = 1
            while target.exists():
                stem, suffix = os.path.splitext(name)
                target = directory / f"{stem} ({index}){suffix}"
                index += 1
            size = 0
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                while chunk := await upload.read(CHUNK):
                    size += len(chunk)
                    if size > MAX_ATTACHMENT_BYTES:
                        raise AttachmentLimitError(
                            f"「{_display_name(name)}」超过单个附件 20MB 的限制"
                        )
                    total += len(chunk)
                    if total > MAX_ATTACHMENT_TOTAL_BYTES:
                        raise AttachmentLimitError("一条消息的附件总大小不能超过 50MB")
                    output.write(chunk)
            items.append(
                StagedUpload(
                    original_name=original,
                    stored_name=name,
                    path=target,
                    size=size,
                    mime_type=_mime_type(name, upload.content_type),
                    is_image=looks_like_image(name, upload.content_type),
                )
            )
        return StagedUploads(directory, items)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _create_unique(directory: Path, name: str) -> tuple[Path, int]:
    stem, suffix = os.path.splitext(name)
    for index in range(10000):
        candidate = name if index == 0 else f"{stem} ({index}){suffix}"
        path = directory / candidate
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            continue
        return path, descriptor
    raise AttachmentStorageError("too many files with the same name")


def store_uploads(root: Path, conversation_id: str, staged: list[StagedUpload]):
    """Copy staged files into the conversation workspace without overwriting."""
    from .agent_types import AttachmentInput

    directory = attachment_directory(root, conversation_id)
    stored: list[AttachmentInput] = []
    created: list[Path] = []
    try:
        for item in staged:
            destination, descriptor = _create_unique(directory, item.stored_name)
            created.append(destination)
            with os.fdopen(descriptor, "wb") as output, item.path.open("rb") as source:
                shutil.copyfileobj(source, output, CHUNK)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(destination, 0o600)
            stored.append(
                AttachmentInput(
                    name=destination.name,
                    path=f"attachments/{destination.name}",
                    size=item.size,
                    mime_type=item.mime_type,
                    is_image=item.is_image,
                )
            )
        return stored
    except BaseException:
        for path in created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
