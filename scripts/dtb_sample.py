#!/usr/bin/env python3
"""Build a budgeted real-data DTB sample that the dtb and markdown plugins read."""

from __future__ import annotations

import argparse
import json
import shutil
import warnings
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from dtb_common import doc_table_name, normalize_table, read_jsonl, write_json
from openpyxl import Workbook, load_workbook

from semantica_dataagent.markdown import chunks

RAW_ROOT = Path("/mnt/d/dataagent-backups/knowledge_base")
SNAPSHOT_PATH = Path("warehouse/raw/dtb")
FACTS = Path("warehouse/facts/_catalog")
DEFAULT_OUTPUT = Path("data/dtb-sample")
MAX_VECTORS = 5400


def seed_tables(dictionary_dir):
    seeds = {
        name
        for path in sorted(Path(dictionary_dir).glob("*.md"))
        if (name := doc_table_name(path.name))
    }
    if not seeds:
        raise ValueError(f"No database.table.md documents found in {dictionary_dir}")
    return seeds


def upstream_closure(seeds, edges, depth):
    upstream = defaultdict(set)
    for edge in edges:
        source = normalize_table(edge.get("source_table"))
        target = normalize_table(edge.get("target_table"))
        if source and target and source != target:
            upstream[target].add(source)
    seen, frontier = set(seeds), set(seeds)
    levels = [sorted(seeds)]
    for _ in range(depth):
        found = set()
        for table in frontier:
            found.update(upstream.get(table, ()))
        found -= seen
        levels.append(sorted(found))
        seen.update(found)
        frontier = found
    return seen, levels


def select_tasks(tasks, tables, include_upstream=True):
    by_id = {str(task.get("task_id")): task for task in tasks if task.get("task_id")}
    producers = {
        task_id
        for task_id, task in by_id.items()
        if any(
            normalize_table(name) in tables for name in task.get("destinations") or []
        )
    }
    selected = set(producers)
    if include_upstream:
        for task_id in producers:
            for upstream in by_id[task_id].get("upstream_tasks") or ():
                selected.add(str(upstream))
    return selected, producers


def copy_tasks(snapshot, output, selected):
    payload = json.loads((snapshot / "任务信息.json").read_text(encoding="utf-8"))
    columns = payload.get("columns")
    rows = payload.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError(  # noqa: TRY004 - file contents, not an argument type
            "任务信息.json must contain columns and data arrays"
        )
    names = [column.get("name") for column in columns]
    try:
        index = names.index("task_id")
    except ValueError as exc:
        raise ValueError("任务信息.json has no task_id column") from exc
    kept = [
        row for row in rows if index < len(row) and str(row[index]).strip() in selected
    ]
    found = {str(row[index]).strip() for row in kept}
    write_json(output / "任务信息.json", {"columns": columns, "data": kept})
    return len(kept), sorted(selected - found)


def copy_tables(snapshot, output, tables):
    source = snapshot / "表信息.xlsx"
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Workbook contains no default style",
            category=UserWarning,
        )
        book = load_workbook(source, read_only=True, data_only=True)
    try:
        values = book.active.values
        try:
            header = list(next(values))
        except StopIteration:
            raise ValueError(f"Table info workbook is empty: {source}") from None
        try:
            database_index = header.index("database_name")
            table_index = header.index("table_name")
        except ValueError as exc:
            raise ValueError(
                "Table info workbook lacks database/table columns"
            ) from exc
        rows = []
        seen_tables = set()
        for row in values:
            values_row = list(row)
            database = values_row[database_index] if database_index < len(row) else None
            table = values_row[table_index] if table_index < len(row) else None
            name = normalize_table(f"{database}.{table}")
            if name and name in tables:
                rows.append(values_row)
                seen_tables.add(name)
    finally:
        book.close()
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Sheet1")
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    workbook.save(output / "表信息.xlsx")
    return len(rows), sorted(seen_tables)


def copy_dimensions(raw_root, output, tables, row_limit):
    root = raw_root / "master_data/raw/dimensions"
    copied_tables, copied_rows = [], 0
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        meta_path = directory / "meta.json"
        rows_path = directory / "current/rows.jsonl"
        if not meta_path.is_file() or not rows_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        database = str(meta.get("database") or "").strip()
        table = str(meta.get("table") or meta.get("qualified_table") or "").strip()
        if "." in table:
            database, table = table.rsplit(".", 1)
        elif not database and "." in str(meta.get("qualified_table") or ""):
            database = str(meta["qualified_table"]).rsplit(".", 1)[0]
        qualified = normalize_table(f"{database}.{table or directory.name}")
        if qualified not in tables:
            continue
        lines = [
            line
            for line in rows_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        kept = lines[:row_limit]
        destination = output / "dimensions" / directory.name
        (destination / "current").mkdir(parents=True)
        (destination / "current/rows.jsonl").write_text(
            "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8"
        )
        sample_meta = dict(meta)
        sample_meta.pop("rows_sha256", None)
        sample_meta.pop("segments", None)
        sample_meta["record_count"] = len(kept)
        sample_meta["truncated"] = bool(meta.get("truncated")) or len(kept) < len(lines)
        sample_meta["sample_row_limit"] = row_limit
        write_json(destination / "meta.json", sample_meta)
        copied_tables.append(qualified)
        copied_rows += len(kept)
    return copied_tables, copied_rows


def _truncate_lines(content, char_limit):
    if char_limit is None or len(content) <= char_limit:
        return content
    cut = content[:char_limit]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0] + "\n"
    return cut


def copy_documents(source, destination, char_limit=None):
    documents = chunk_count = 0
    for path in sorted(Path(source).rglob("*.md")):
        content = _truncate_lines(path.read_text(encoding="utf-8"), char_limit)
        target = Path(destination) / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        documents += 1
        chunk_count += len(chunks(content))
    return documents, chunk_count


def estimate_vectors(counts, rate_min=2.0, rate_max=3.0):
    vectors = counts["tables"] + counts["fields"] + counts["tasks"] + counts["chunks"]
    return {
        "vectors": vectors,
        "minutes_at_2_per_second": round(vectors / rate_min / 60, 1),
        "minutes_at_3_per_second": round(vectors / rate_max / 60, 1),
    }


def _reset(path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def build_sample(
    raw_root,
    output,
    depth=2,
    dimension_row_limit=200,
    metric_char_limit=300,
    include_upstream_tasks=True,
    max_vectors=MAX_VECTORS,
):
    raw_root = Path(raw_root)
    output = Path(output)
    snapshot = raw_root / SNAPSHOT_PATH
    snapshots = sorted(
        path
        for path in snapshot.iterdir()
        if path.is_dir() and (path / "任务信息.json").is_file()
    )
    if not snapshots:
        raise ValueError(f"No DTB snapshot found under {snapshot}")
    current = snapshots[-1]
    seeds = seed_tables(raw_root / "quick_base/air/字典")
    edges = read_jsonl(raw_root / FACTS / "lineage_edges.jsonl")
    facts_tasks = read_jsonl(raw_root / FACTS / "tasks.jsonl")
    tables, levels = upstream_closure(seeds, edges, depth)
    selected_tasks, producers = select_tasks(
        facts_tasks, tables, include_upstream_tasks
    )

    temporary = output.with_name(output.name + ".tmp")
    _reset(temporary)
    dtb_out = temporary / "dtb"
    dtb_out.mkdir()
    tasks, missing_tasks = copy_tasks(current, dtb_out, selected_tasks)
    fields, tables_with_metadata = copy_tables(current, dtb_out, tables)
    dimensions, dimension_rows = copy_dimensions(
        raw_root, temporary, tables, dimension_row_limit
    )
    documents = chunks_count = 0
    for source, destination, limit in (
        (raw_root / "quick_base/air/字典", temporary / "knowledge/字典", None),
        (
            raw_root / "quick_base/air/指标",
            temporary / "knowledge/指标",
            metric_char_limit,
        ),
        (raw_root / "business_base", temporary / "knowledge/业务知识", None),
    ):
        found, found_chunks = copy_documents(source, destination, limit)
        documents += found
        chunks_count += found_chunks

    counts = {
        "tables": len(tables_with_metadata),
        "fields": fields,
        "tasks": tasks,
        "dimension_tables": len(dimensions),
        "dimension_rows": dimension_rows,
        "documents": documents,
        "chunks": chunks_count,
    }
    estimate = estimate_vectors(counts)
    manifest = {
        "schema_version": 1,
        "version": "sample-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(UTC).isoformat(),
        "source_snapshot": current.name,
        "selection": {
            "seed_tables": sorted(seeds),
            "depth": depth,
            "tables_in_closure": len(tables),
            "tables_with_metadata": len(tables_with_metadata),
            "producer_tasks": len(producers),
            "include_upstream_tasks": include_upstream_tasks,
            "tasks": tasks,
            "missing_tasks": missing_tasks,
            "dimension_row_limit": dimension_row_limit,
            "metric_char_limit": metric_char_limit,
            "documents": documents,
            "levels": [len(level) for level in levels],
        },
        "counts": counts,
        "embedding": estimate,
        "doc_policy": (
            "dictionary and business documents are complete; metric documents "
            f"keep the first {metric_char_limit} characters at a line boundary"
        ),
    }
    write_json(dtb_out / "manifest.json", manifest)
    if estimate["vectors"] > max_vectors:
        shutil.rmtree(temporary)
        raise ValueError(
            f"Sample needs {estimate['vectors']} vectors, above budget {max_vectors}"
        )
    if output.exists():
        shutil.rmtree(output)
    temporary.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=RAW_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--dimension-row-limit", type=int, default=200)
    parser.add_argument("--metric-char-limit", type=int, default=300)
    parser.add_argument("--max-vectors", type=int, default=MAX_VECTORS)
    parser.add_argument("--producers-only", action="store_true")
    args = parser.parse_args()
    manifest = build_sample(
        args.raw_root,
        args.output,
        depth=args.depth,
        dimension_row_limit=args.dimension_row_limit,
        metric_char_limit=args.metric_char_limit,
        include_upstream_tasks=not args.producers_only,
        max_vectors=args.max_vectors,
    )
    counts = manifest["counts"]
    estimate = manifest["embedding"]
    print(f"output: {args.output}")
    print(
        "tables: {tables} (closure {closure}; {fields} fields); tasks: {tasks}; "
        "dimensions: {dimension_tables} tables / {dimension_rows} rows".format(
            closure=manifest["selection"]["tables_in_closure"], **counts
        )
    )
    print(
        "documents: {documents}; chunks: {chunks}; vectors: {vectors}".format(
            **counts, **estimate
        )
    )
    print(
        "embedding estimate: {minutes_at_3_per_second} min at 3 texts/s, "
        "{minutes_at_2_per_second} min at 2 texts/s".format(**estimate)
    )
    print("manifest: " + str(Path(args.output) / "dtb/manifest.json"))


if __name__ == "__main__":
    main()
