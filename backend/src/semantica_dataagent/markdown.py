"""Markdown source adapter and deterministic, line-addressable chunks."""

import hashlib
import re
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

from jsonschema import validate

from .canonical import ReadResult, Record


def doc_ref(category, path):
    return "document:" + quote(category, safe="") + "/" + quote(path, safe="/")


def table_name(path):
    match = re.fullmatch(r"([^. /]+)\.([^. /]+)\.md", Path(path).name, re.IGNORECASE)
    return match.groups() if match else None


def chunks(content, limit=900):
    lines = content.splitlines()
    result, section = [], []
    pending, start = [], 0

    def flush(end):
        nonlocal pending
        if pending:
            result.append(
                {
                    "content": "\n".join(pending),
                    "section": section.copy(),
                    "start_line": start,
                    "end_line": end,
                }
            )
            pending = []

    i = 0
    while i < len(lines):
        line = lines[i]
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush(i)
            level = len(heading.group(1))
            section = section[: level - 1] + [heading.group(2)]
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            flush(i)
            j = i + 1
            while j < len(lines) and not re.match(
                r"^\s*"
                + re.escape(fence.group(1)[0])
                + "{"
                + str(len(fence.group(1)))
                + r",}\s*$",
                lines[j],
            ):
                j += 1
            closed = j < len(lines)
            close = lines[j] if closed else fence.group(1)
            end = j
            k = i + 1
            while k < end:
                begin = k
                group = [line]
                size = len(line) + len(close) + 2
                while k < end and (size + len(lines[k]) + 1 <= limit or k == begin):
                    group.append(lines[k])
                    size += len(lines[k]) + 1
                    k += 1
                start = i + 1 if begin == i + 1 else begin + 1
                pending = group + [close]
                flush(j + 1 if k == end and closed else k)
            if k == i + 1:
                start = i + 1
                pending = [line, close]
                flush(j + 1 if closed else j)
            i = j + 1 if closed else j
            continue
        if (
            i + 1 < len(lines)
            and "|" in line
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1])
        ):
            flush(i)
            header = lines[i : i + 2]
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                j += 1
            k = i + 2
            while k < j:
                begin = k
                group = header.copy()
                while k < j and (
                    len("\n".join(group)) + len(lines[k]) + 1 <= limit or k == begin
                ):
                    group.append(lines[k])
                    k += 1
                start = i + 1 if begin == i + 2 else begin + 1
                pending = group
                flush(k)
            if j == i + 2:
                start = i + 1
                pending = header
                flush(j)
            i = j
            continue
        if pending and (
            not line.strip() or sum(len(x) + 1 for x in pending) + len(line) > limit
        ):
            flush(i)
        if line.strip():
            if not pending:
                start = i + 1
            pending.append(line)
        i += 1
    flush(len(lines))
    return _split_oversized(result, limit)


def _split_oversized(parts, limit):
    """Split chunks that still exceed the limit, keeping table headers."""
    result = []
    for part in parts:
        content = part["content"]
        if len(content) <= limit:
            result.append(part)
            continue
        lines = content.splitlines()
        prefix, body = [], lines
        if (
            len(lines) >= 3
            and "|" in lines[0]
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[1])
        ):
            prefix, body = lines[:2], lines[2:]
        prefix_text = "\n".join(prefix)
        available = limit - len(prefix_text) - (1 if prefix else 0)
        if available <= 0:
            prefix, body = [], lines
            prefix_text = ""
            available = limit
        pieces, current = [], []

        for line in body:
            if len(line) > available:
                _flush_piece(pieces, current, prefix)
                current = []
                pieces.extend(
                    "\n".join(prefix + [line[start : start + available]])
                    if prefix
                    else line[start : start + available]
                    for start in range(0, len(line), available)
                )
                continue
            candidate = current + [line]
            text = "\n".join(prefix + candidate) if prefix else "\n".join(candidate)
            if current and len(text) > limit:
                _flush_piece(pieces, current, prefix)
                current = [line]
            else:
                current = candidate
        _flush_piece(pieces, current, prefix)
        result.extend({**part, "content": text} for text in pieces)
    return result


def _flush_piece(pieces, current, prefix):
    if current:
        pieces.append("\n".join(prefix + current) if prefix else "\n".join(current))


def section_range(lines, section):
    path = []
    found = 0
    start = 0
    fence = None
    for i, line in enumerate(lines):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        level = len(match.group(1))
        path = path[: level - 1] + [match.group(2)]
        if found and level <= found:
            return start, i
        if not found and (match.group(2) == section or " / ".join(path) == section):
            start, found = i, level
    if not found:
        raise ValueError("Section not found")
    return start, len(lines)


class MarkdownAdapter:
    id = "markdown"
    version = "1.0.0"
    display_name = "Markdown"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {"path": {"type": "string", "default": "knowledge"}},
        "additionalProperties": False,
    }

    def read(self, directory, parameters):
        validate(parameters, self.parameters_schema)
        relative = Path(parameters.get("path", "knowledge"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Markdown path must be relative to source directory")
        root = directory / relative
        if not root.exists():
            return ReadResult([], {})
        if not root.is_dir():
            raise ValueError("Markdown root is not a directory")
        records, hashes = [], {}
        for category in sorted(p for p in root.iterdir() if p.is_dir()):
            docs = []
            for path in sorted(category.rglob("*.md")):
                if not path.is_file():
                    continue
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("Markdown file escapes source directory")
                raw = path.read_bytes()
                hashes[str(path.relative_to(directory))] = hashlib.sha256(
                    raw
                ).hexdigest()
                docs.append(
                    {
                        "category": category.name,
                        "path": path.relative_to(category).as_posix(),
                        "content": raw.decode("utf-8"),
                    }
                )
            if not any(d["path"] == "index.md" for d in docs):
                listing = []
                for doc in docs:
                    title = next(
                        (
                            line[2:].strip()
                            for line in doc["content"].splitlines()
                            if line.startswith("# ")
                        ),
                        doc["path"],
                    )
                    listing.append(f"- {doc['path']}：{title}")
                docs.append(
                    {
                        "category": category.name,
                        "path": "index.md",
                        "content": "# 自动生成的索引地图\n\n自动生成\n\n"
                        + "\n".join(listing),
                        "generated": True,
                    }
                )
            records.extend(Record("document", d) for d in docs)
        return ReadResult(records, hashes)
