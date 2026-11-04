"""DTB task parsers for shell jobs (Livy/beeline) and merge-onself jobs."""

import json
import re
from typing import ClassVar

from .canonical import Record
from .plugins import ParseResult
from .sql_tasks import SQLTaskParser

_ASSIGNMENT = re.compile(
    r"(?m)^[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\n;]*)"
)
_VARIABLE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
)
_LIVY_CALL = re.compile(
    r"(?mi)^[ \t]*submit_sql_and_wait[ \t]+"
    r"(?P<argument>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s;]+)"
)
_BEELINE_E = re.compile(
    r"(?m)\bbeeline\b[^\n]*?(?<![\w-])-e(?:[ \t]*=[ \t]*|[ \t]+)"
    r"(?P<argument>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s;]+)"
)
_TEMP_VIEW = re.compile(
    r"(?is)\bcreate\s+(?:or\s+replace\s+)?temporary\s+view\s+"
    r"(?:if\s+not\s+exists\s+)?(?P<name>[A-Za-z_][\w$]*)\b(?P<body>[^;]*)"
)
_DBTABLE = re.compile(
    r"(?i)\bdbtable\s*['\"]?\\?`?(?P<database>[A-Za-z_][\w$-]*)\\?`?\s*\.\s*"
    r"\\?`?(?P<table>[A-Za-z_][\w$-]*)"
)
_FROM_JOIN = re.compile(r"(?i)\b(?:from|join)\s+([A-Za-z_][\w$-]*\.[A-Za-z_][\w$-]*)")
_WRITE = re.compile(
    r"(?i)\b(?:"
    r"insert\s+(?:overwrite\s+|into\s+)*table\s+"
    r"|insert\s+(?:overwrite\s+)?into\s+"
    r"|create\s+(?:or\s+replace\s+)?(?:external\s+)?table\s+"
    r"(?:if\s+not\s+exists\s+)?"
    r")([A-Za-z_][\w$-]*\.[A-Za-z_][\w$-]*)"
)

_SKIP_SQL = re.compile(r"(?is)^\s*(?:show|set|use|msck|add|dfs|describe|desc|reset)\b")
_SQL = SQLTaskParser()


def _text(value):
    if value is None:
        return ""
    return str(value)


def _strip_quotes(value):
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _assignments(source):
    return {
        match.group("name"): _strip_quotes(match.group("value"))
        for match in _ASSIGNMENT.finditer(source)
    }


def _expand_variable(name, assignments, cache, stack=()):
    if name in cache:
        return cache[name]
    if name not in assignments or name in stack:
        return None
    raw = assignments[name]

    def replace(match):
        child = match.group("braced") or match.group("plain")
        expanded = _expand_variable(child, assignments, cache, stack + (name,))
        return match.group(0) if expanded is None else expanded

    expanded = _VARIABLE.sub(replace, raw)
    cache[name] = expanded
    return expanded


def _expand(text, assignments, cache):
    def replace(match):
        name = match.group("braced") or match.group("plain")
        expanded = _expand_variable(name, assignments, cache)
        return match.group(0) if expanded is None else expanded

    return _VARIABLE.sub(replace, text)


def _parse_sql(sql, name):
    task = Record(
        "task",
        {"name": name, "type": "spark", "dialect": "spark", "sql": sql},
    )
    return _SQL.parse(task, {})


def _normalize_sql(sql):
    sql = re.sub(r"\$\([^)]*\)", "0", sql)
    sql = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "0", sql)
    return re.sub(r"\{\{.*?\}\}", "0", sql, flags=re.DOTALL)


def _clean_table(name):
    text = name.strip().strip("`\"'")
    if text.lower().startswith("<default>."):
        text = text.split(".", 1)[1]
    return text.strip()


def _dbtables(text):
    return [
        f"{_clean_table(match.group('database'))}.{_clean_table(match.group('table'))}"
        for match in _DBTABLE.finditer(text)
    ]


def _temp_views(text):
    result = {}
    for match in _TEMP_VIEW.finditer(text):
        tables = _dbtables(match.group("body"))
        if tables:
            result[match.group("name")] = tables[0]
    return result


def _basic_tables(source):
    sources, targets = set(), set()
    variables = _assignments(source)
    origin_schema = variables.get("origin_schema")
    origin_table = variables.get("origin_table")
    if origin_schema and origin_table:
        sources.add(f"{origin_schema}.{origin_table}")
    target_schema = variables.get("target_schema")
    target_table = variables.get("target_table")
    if target_schema and target_table:
        targets.add(f"{target_schema}.{target_table}")
    sources.update(
        _clean_table(match.group(1)) for match in _FROM_JOIN.finditer(source)
    )
    targets.update(_clean_table(match.group(1)) for match in _WRITE.finditer(source))
    return sources, targets


def _add_sql(result, reads, writes, edges, views):
    before = (len(reads), len(writes), len(edges))
    for name in result.reads:
        cleaned = _clean_table(name)
        reads.add(views.get(cleaned, cleaned))
    writes.update(_clean_table(name) for name in result.writes)
    for source, target in result.edges or []:
        source = _clean_table(source)
        edges.add((views.get(source, source), _clean_table(target)))
    return (len(reads), len(writes), len(edges)) != before


def _submission_sql(match, assignments, cache, unresolved):
    argument = _strip_quotes(match.group("argument"))
    variable = _VARIABLE.fullmatch(argument)
    if variable:
        name = variable.group("braced") or variable.group("plain")
        resolved = _expand_variable(name, assignments, cache)
        if resolved is None:
            unresolved.add(name)
            return None
        return _expand(resolved, assignments, cache).strip()
    return _expand(argument, assignments, cache).strip()


class DtbTaskParser:
    id = "dtb"
    version = "1.0.0"
    display_name = "DTB tasks"
    parameters_schema: ClassVar[dict] = {
        "type": "object",
        "additionalProperties": False,
    }
    task_types: ClassVar[list[str]] = ["shell", "merge_onself"]

    def parse(self, task, parameters):
        task_type = _text(task.data.get("type")).strip().lower()
        if task_type == "merge_onself":
            return self._merge_onself(task)
        if task_type == "shell":
            return self._shell(task)
        return ParseResult([], [], "unsupported", f"Unsupported task type: {task_type}")

    @staticmethod
    def _merge_onself(task):
        parameters = task.data.get("parameters")
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except json.JSONDecodeError:
                parameters = None
        table = ""
        if isinstance(parameters, dict):
            nested = parameters.get("params")
            if isinstance(nested, dict):
                table = _clean_table(_text(nested.get("table")))
            if not table:
                table = _clean_table(_text(parameters.get("table")))
        if not table:
            return ParseResult([], [], "failed", "merge_onself target is unknown")
        return ParseResult([table], [table], "success", "merge_onself", [])

    def _shell(self, task):
        source = _text(task.data.get("sql"))
        name = _text(task.data.get("name")) or "shell"
        assignments = _assignments(source)
        cache = {}
        expanded = _expand(source, assignments, cache)
        basic_sources, basic_targets = _basic_tables(expanded)
        jdbc_sources = set(_dbtables(expanded))
        views = _temp_views(expanded)

        reads, writes, edges = set(), set(), set()
        unresolved = set()
        parsed_reasons = set()
        failed_sql = 0

        for match in _LIVY_CALL.finditer(source):
            sql = _submission_sql(match, assignments, cache, unresolved)
            if not sql or _SKIP_SQL.match(_normalize_sql(sql)):
                continue
            if _TEMP_VIEW.search(sql) and not _WRITE.search(sql) and _dbtables(sql):
                continue
            parsed = _parse_sql(sql, name)
            if parsed.status == "success":
                if _add_sql(parsed, reads, writes, edges, views):
                    parsed_reasons.add("livy")
                continue
            parsed = _parse_sql(_normalize_sql(sql), name)
            if parsed.status == "success":
                if _add_sql(parsed, reads, writes, edges, views):
                    parsed_reasons.add("livy")
            elif not (_TEMP_VIEW.search(sql) and not _WRITE.search(sql)):
                failed_sql += 1

        for match in _BEELINE_E.finditer(source):
            sql = _submission_sql(match, assignments, cache, unresolved)
            if not sql or _SKIP_SQL.match(_normalize_sql(sql)):
                continue
            parsed = _parse_sql(sql, name)
            if parsed.status == "success":
                if _add_sql(parsed, reads, writes, edges, views):
                    parsed_reasons.add("beeline")
            else:
                parsed = _parse_sql(_normalize_sql(sql), name)
                if parsed.status == "success":
                    if _add_sql(parsed, reads, writes, edges, views):
                        parsed_reasons.add("beeline")
                else:
                    failed_sql += 1

        reads.update(basic_sources)
        writes.update(basic_targets)
        if jdbc_sources:
            reads.update(jdbc_sources)
        edges.discard(("", ""))
        if not edges:
            for upstream in reads:
                for downstream in writes:
                    if upstream != downstream:
                        edges.add((upstream, downstream))

        reason = "+".join(sorted(parsed_reasons))
        if not reason:
            reason = "shell_basic"
            if failed_sql:
                reason = "shell_basic (sql parse failed)"
        if not reads and writes:
            return ParseResult(
                sorted(reads), sorted(writes), "failed", "No upstream tables recognized"
            )
        if not reads and not writes:
            return ParseResult([], [], "failed", "No tables recognized in shell script")
        return ParseResult(
            sorted(reads), sorted(writes), "success", reason, sorted(edges)
        )
