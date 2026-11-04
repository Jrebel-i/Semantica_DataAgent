#!/usr/bin/env python3
"""Compare DTB parsed lineage with the DataAgent facts layer on a full snapshot."""

from __future__ import annotations

import argparse
import logging
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dtb_common import normalize_table, read_jsonl, write_json

from semantica_dataagent.dtb_source import DtbAdapter
from semantica_dataagent.dtb_tasks import DtbTaskParser
from semantica_dataagent.sql_tasks import SQLTaskParser

RAW_ROOT = Path("/mnt/d/dataagent-backups/knowledge_base")
FACTS = Path("warehouse/facts/_catalog")
DEFAULT_MARKDOWN = Path("data/lineage-compare.md")
DEFAULT_JSON = Path("data/lineage-compare.json")
TASK_TYPES = ("spark", "shell", "merge_onself")


def _type(value):
    return str(value or "").replace(" ", "_").lower()


def _edges(result):
    if result.edges is not None:
        return set(result.edges)
    return {(source, target) for source in result.reads for target in result.writes}


def _clean_edges(edges):
    cleaned = set()
    for source, target in edges:
        left, right = normalize_table(source), normalize_table(target)
        if left and right and left != right:
            cleaned.add((left, right))
    return cleaned


def parse_snapshot(raw_root):
    raw_root = Path(raw_root)
    adapter = DtbAdapter()
    result = adapter.read(
        raw_root / "warehouse/raw",
        {
            "snapshot_path": "dtb",
            "dimensions_path": "__lineage_compare_missing__",
            "dimension_tables": [],
            "dimension_row_limit": 1,
        },
    )
    sql, dtb = SQLTaskParser(), DtbTaskParser()
    parsed = defaultdict(set)
    edge_tasks = defaultdict(set)
    statuses = Counter()
    failures = []
    for task in (record for record in result.records if record.kind == "task"):
        task_type = _type(task.data.get("type"))
        parser = sql if task_type == "spark" else dtb
        parsed_result = parser.parse(task, {})
        statuses[(task_type, parsed_result.status)] += 1
        if parsed_result.status == "failed":
            failures.append(
                {
                    "task": task.data.get("name", ""),
                    "type": task_type,
                    "reason": (parsed_result.reason or "")[:500],
                }
            )
        for source, target in _clean_edges(_edges(parsed_result)):
            parsed[task_type].add((source, target))
            parsed["all"].add((source, target))
            edge_tasks[(source, target)].add(task.data.get("name", ""))
    return result, parsed, edge_tasks, statuses, failures


def load_facts(raw_root):
    facts = read_jsonl(Path(raw_root) / FACTS / "lineage_edges.jsonl")
    tasks = read_jsonl(Path(raw_root) / FACTS / "tasks.jsonl")
    by_id = {str(task.get("task_id")): task for task in tasks if task.get("task_id")}
    typed = defaultdict(set)
    evidence = defaultdict(set)
    info = defaultdict(list)
    facts_all = set()
    for edge in facts:
        source, target = (
            normalize_table(edge.get("source_table")),
            normalize_table(edge.get("target_table")),
        )
        if not source or not target or source == target:
            continue
        task_id = str(edge.get("task_id") or "")
        task = by_id.get(task_id, {})
        task_type = _type(task.get("task_type"))
        if task_type == "spark_sql":
            task_type = "spark"
        task_type = task_type or "unknown"
        sources = "+".join(sorted(edge.get("evidence_sources") or []))
        typed[task_type].add((source, target))
        typed["all"].add((source, target))
        evidence[sources].add((source, target))
        facts_all.add((source, target))
        info[(source, target)].append(
            {
                "task_id": task_id,
                "task_name": task.get("task_name", ""),
                "task_type": task_type,
                "evidence": sources,
            }
        )
    return typed, evidence, info, len(facts)


def score(parsed, facts):
    intersection = len(parsed & facts)
    return {
        "parsed": len(parsed),
        "facts": len(facts),
        "intersection": intersection,
        "missing": len(facts - parsed),
        "extra": len(parsed - facts),
        "recall": round(intersection / len(facts), 4) if facts else None,
        "precision": round(intersection / len(parsed), 4) if parsed else None,
        "f1": round(2 * intersection / (len(parsed) + len(facts)), 4)
        if parsed and facts
        else None,
    }


def compare(raw_root, max_examples=20):
    result, parsed, edge_tasks, statuses, failures = parse_snapshot(raw_root)
    facts, evidence, fact_info, raw_fact_rows = load_facts(raw_root)
    by_type = {
        task_type: score(parsed.get(task_type, set()), facts.get(task_type, set()))
        for task_type in TASK_TYPES
    }
    by_evidence = {
        name: score(parsed["all"], values) for name, values in sorted(evidence.items())
    }
    missing_examples = []
    for source, target in sorted(facts["all"] - parsed["all"])[:max_examples]:
        entries = fact_info.get((source, target)) or [{}]
        missing_examples.append(
            {"upstream": source, "downstream": target, **entries[0]}
        )
    extra_examples = []
    for source, target in sorted(parsed["all"] - facts["all"])[:max_examples]:
        extra_examples.append(
            {
                "upstream": source,
                "downstream": target,
                "tasks": sorted(edge_tasks.get((source, target), set()))[:5],
            }
        )
    status_rows = [
        {"type": task_type, "status": status, "count": count}
        for (task_type, status), count in sorted(statuses.items())
    ]
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "snapshot": result.stats.get("snapshot_directories"),
        "adapter_stats": result.stats,
        "fact_rows": raw_fact_rows,
        "overall": score(parsed["all"], facts["all"]),
        "by_type": by_type,
        "by_evidence": by_evidence,
        "parse_status": status_rows,
        "failure_examples": failures[:max_examples],
        "missing_examples": missing_examples,
        "extra_examples": extra_examples,
        "method": {
            "normalization": "lower-case database.table; default databases removed; self edges dropped",
            "parsed": "spark via SQLTaskParser; shell and merge_onself via DtbTaskParser",
            "facts": "lineage_edges.jsonl; facts self edges are excluded",
        },
    }
    return report


def render_markdown(report):
    lines = [
        "# DTB lineage comparison",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Facts rows (raw): {report['fact_rows']}",
        f"- Facts edges after removing self edges: {report['overall']['facts']}",
        "- Parsed: spark via `SQLTaskParser`; shell/merge_onself via `DtbTaskParser`",
        "- Normalization: lower-case `database.table`, remove `default` databases, drop self edges",
        "",
        "## Overall",
        "",
        "| parsed | facts | intersection | recall | precision | F1 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    overall = report["overall"]
    lines.append(
        "| {parsed} | {facts} | {intersection} | {recall} | {precision} | {f1} |".format(
            **overall
        )
    )
    lines += [
        "",
        "## By task type",
        "",
        "| type | parsed | facts | intersection | recall | precision | F1 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task_type, values in report["by_type"].items():
        lines.append(
            f"| {task_type} | {values['parsed']} | {values['facts']} | "
            f"{values['intersection']} | {values['recall']} | {values['precision']} | "
            f"{values['f1']} |"
        )
    lines += [
        "",
        "## Recall by facts evidence",
        "",
        "| evidence | parsed | facts | intersection | recall |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, values in report["by_evidence"].items():
        lines.append(
            f"| {name} | {values['parsed']} | {values['facts']} | "
            f"{values['intersection']} | {values['recall']} |"
        )
    lines += [
        "",
        "## Parse status",
        "",
        "| type | status | count |",
        "| --- | --- | --- |",
    ]
    for row in report["parse_status"]:
        lines.append(f"| {row['type']} | {row['status']} | {row['count']} |")
    lines += [
        "",
        f"## Missing examples (first {len(report['missing_examples'])})",
        "",
        "| upstream | downstream | task | type | evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["missing_examples"]:
        lines.append(
            f"| {row.get('upstream')} | {row.get('downstream')} | "
            f"{row.get('task_name', '')} | {row.get('task_type', '')} | "
            f"{row.get('evidence', '')} |"
        )
    lines += [
        "",
        f"## Extra examples (first {len(report['extra_examples'])})",
        "",
        "| upstream | downstream | tasks |",
        "| --- | --- | --- |",
    ]
    for row in report["extra_examples"]:
        lines.append(
            f"| {row['upstream']} | {row['downstream']} | {', '.join(row['tasks'])} |"
        )
    if report["failure_examples"]:
        lines += [
            "",
            "## Parse failures",
            "",
            "| task | type | reason |",
            "| --- | --- | --- |",
        ]
        for row in report["failure_examples"]:
            reason = row["reason"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {row['task']} | {row['type']} | {reason} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=RAW_ROOT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--max-examples", type=int, default=20)
    args = parser.parse_args()
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    report = compare(args.raw_root, args.max_examples)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    write_json(args.json, report)
    overall = report["overall"]
    print(
        "overall: parsed={parsed} facts={facts} intersection={intersection} "
        "recall={recall} precision={precision}".format(**overall)
    )
    for task_type, values in report["by_type"].items():
        print(
            f"{task_type}: parsed={values['parsed']} facts={values['facts']} "
            f"recall={values['recall']} precision={values['precision']}"
        )
    print(f"report: {args.markdown}")


if __name__ == "__main__":
    main()
