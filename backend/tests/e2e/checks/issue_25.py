"""Run the retrieval evaluation on the synthetic demo question set."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
QUESTIONS = REPO / "eval" / "demo" / "questions.yaml"


def _server_environment(ctx) -> dict[str, str]:
    """Copy the running service environment without exposing secrets in results."""
    server = getattr(ctx.runtime, "server", None)
    ctx.require(server is not None and server.pid, "serve process is not running")
    path = Path(f"/proc/{server.pid}/environ")
    ctx.require(path.is_file(), f"cannot read service environment: {path}")
    values: dict[str, str] = {}
    for item in path.read_bytes().split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        values[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    ctx.require(
        "DATAAGENT_ENCRYPTION_KEY" in values,
        "service environment is missing DATAAGENT_ENCRYPTION_KEY",
    )
    environment = os.environ.copy()
    environment["DATAAGENT_ENCRYPTION_KEY"] = values["DATAAGENT_ENCRYPTION_KEY"]
    environment["DATAAGENT_DB_PATH"] = str(ctx.runtime.db_path)
    environment["TMPDIR"] = str(Path(ctx.work_dir) / "tmp")
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return environment


def check_retrieval_eval_on_demo(ctx):
    """Run ``semantica-dataagent eval`` in retrieval mode and validate its report."""
    from semantica_dataagent.eval import load_questions

    ctx.require(QUESTIONS.is_file(), f"demo questions are missing: {QUESTIONS}")
    expected_ids = [question.id for question in load_questions(QUESTIONS)]
    ctx.require(len(expected_ids) == 30, "demo question set must contain 30 questions")

    prefix = Path(ctx.work_dir) / "eval-demo" / "report"
    environment = _server_environment(ctx)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from semantica_dataagent.cli import main; main()",
            "eval",
            "--questions",
            str(QUESTIONS),
            "--report",
            str(prefix),
        ],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    ctx.logger.line(result.stdout.strip())
    if result.returncode != 0:
        ctx.logger.line(result.stderr.strip()[-2000:])
    ctx.require(result.returncode == 0, "retrieval evaluation command failed")

    markdown = Path(str(prefix) + ".md")
    payload = Path(str(prefix) + ".json")
    ctx.require(markdown.is_file(), f"markdown report is missing: {markdown}")
    ctx.require(payload.is_file(), f"json report is missing: {payload}")
    report = json.loads(payload.read_text(encoding="utf-8"))

    ctx.require(report.get("mode") == "retrieval", "report mode is not retrieval")
    metrics = report.get("metrics") or {}
    ctx.require(metrics.get("questions") == 30, "report question count is not 30")
    results = report.get("results") or []
    ctx.require(len(results) == 30, "report does not contain 30 per-question results")
    ctx.require(
        [row.get("id") for row in results] == expected_ids,
        "per-question result ids do not match the question set",
    )
    for row in results:
        ctx.require(row.get("question"), f"{row.get('id')}: missing question")
        ctx.require(
            row.get("expected_tables"), f"{row.get('id')}: missing expected tables"
        )
        ctx.require(
            isinstance(row.get("table_recall_at_5"), (int, float)),
            f"{row.get('id')}: missing table recall",
        )
        ctx.require(
            isinstance(row.get("validate_sql"), dict),
            f"{row.get('id')}: missing validate_sql result",
        )
        ctx.require(
            isinstance(row.get("failure_reasons"), list),
            f"{row.get('id')}: missing failure reasons",
        )

    recall = metrics.get("table_recall_at_5")
    ctx.require(
        isinstance(recall, (int, float)) and recall >= 0.8,
        f"table recall@5 {recall!r} is below 0.80",
    )
    pass_rate = metrics.get("validate_sql_pass_rate")
    ctx.require(
        pass_rate == 1.0,
        f"reference SQL validate_sql pass rate is {pass_rate!r}, expected 1.0",
    )
    return {
        "questions": metrics["questions"],
        "table_recall_at_5": recall,
        "table_hit_rate_at_5": metrics.get("table_hit_rate_at_5"),
        "validate_sql_pass_rate": pass_rate,
        "report_markdown": str(markdown),
        "report_json": str(payload),
    }
