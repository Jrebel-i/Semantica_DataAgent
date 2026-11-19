"""Offline verification tests for Phase 5-9 acceptance fixtures, cases, and scripts."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_DIR = REPO_ROOT / "eval" / "phase-5-9-acceptance"
OSSIE_SAMPLE_DIR = ACCEPTANCE_DIR / "ossie-sample"
CASES_PATH = ACCEPTANCE_DIR / "cases.yaml"
CHECKS_PATH = REPO_ROOT / "backend" / "tests" / "e2e" / "checks" / "phase_5_to_9.py"
VERIFY_SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_phase_5_to_9.py"


def test_phase_5_9_ossie_sample_and_overrides_are_valid() -> None:
    from semantica_dataagent.ossie_source import OssieAdapter, validate_ossie_schema
    from semantica_dataagent.semantic_overrides import (
        load_overrides_file,
        validate_override_document,
    )

    model_path = OSSIE_SAMPLE_DIR / "ossie" / "demo_commerce.yaml"
    assert model_path.is_file()
    raw = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    validate_ossie_schema(raw, context="demo_commerce.yaml")

    result = OssieAdapter().read(
        OSSIE_SAMPLE_DIR, {"path": "ossie", "category": "ossie"}
    )
    tables = [r for r in result.records if r.kind == "table"]
    rels = [r for r in result.records if r.kind == "relationship"]
    docs = [r for r in result.records if r.kind == "document"]
    assert len(tables) == 2
    assert len(rels) == 1
    assert len(docs) >= 1

    overrides_doc, sha = load_overrides_file(OSSIE_SAMPLE_DIR)
    assert sha is not None
    assert validate_override_document(overrides_doc) == []
    assert "hive_prod.dwd.dwd_orders" in overrides_doc["tables"]


def test_phase_5_9_acceptance_cases_glossary_and_schema() -> None:
    text = CASES_PATH.read_text(encoding="utf-8")
    forbidden_terms = ("埋点日志", "审计流水", "隐式外键", "采样入库", "离线爬虫")
    for term in forbidden_terms:
        assert term not in text, f"cases.yaml 含 GLOSSARY 禁用词: {term}"

    cases = yaml.safe_load(text)
    assert isinstance(cases, list) and len(cases) == 25
    required_fields = {
        "id",
        "phase",
        "title",
        "category",
        "execution_mode",
        "preconditions",
        "steps",
        "expected",
        "pass_criteria",
    }
    valid_modes = {
        "harness_e2e",
        "live_cluster_api",
        "live_cluster_dtb",
        "live_cluster_llm",
        "manual_ui",
    }
    seen_ids: set[str] = set()
    seen_phases: set[int] = set()
    for row in cases:
        assert required_fields <= set(row), f"用例缺少字段: {row}"
        assert row["id"] not in seen_ids
        assert row["execution_mode"] in valid_modes
        seen_ids.add(row["id"])
        seen_phases.add(row["phase"])
    assert seen_phases == {5, 6, 7, 8, 9}


def test_e2e_checks_module_imports_and_runs_phase6_offline(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("phase_5_to_9_checks", CHECKS_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    expected_funcs = [
        "check_phase5_ossie_import_and_export",
        "check_phase6_dtb_to_ossie_conversion",
        "check_phase7_project_isolation_and_lifecycle",
        "check_phase8_overrides_eval_and_evolution_apis",
        "check_phase9_discovery_config_and_guardrails",
    ]
    for fn_name in expected_funcs:
        assert callable(getattr(mod, fn_name, None)), f"缺少检查函数: {fn_name}"

    def _require(cond: object, msg: str) -> None:
        if not cond:
            raise AssertionError(msg)

    fake_ctx = SimpleNamespace(work_dir=tmp_path, require=_require)
    summary = mod.check_phase6_dtb_to_ossie_conversion(fake_ctx)
    assert summary["datasets"] >= 3
    assert summary["relationships"] >= 2
    assert summary["high_cardinality_rows"] >= 25


def test_verify_phase_5_to_9_cli_self_test_and_dry_run_without_secret_leak() -> None:
    secret_tok = "tok_should_never_leak_987654"
    secret_dtb = "dtb_should_never_leak_123456"
    env = {
        **os.environ,
        "DATAAGENT_TOKEN": secret_tok,
        "DATAAGENT_PERSONAL_DTB_TOKEN": secret_dtb,
    }

    self_test_proc = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT_PATH), "--self-test"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert self_test_proc.returncode == 0, self_test_proc.stderr
    assert secret_tok not in self_test_proc.stdout + self_test_proc.stderr
    assert secret_dtb not in self_test_proc.stdout + self_test_proc.stderr

    dry_proc = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT_PATH),
            "--dry-run",
            "--phases",
            "all",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert dry_proc.returncode == 0, dry_proc.stderr
    assert secret_tok not in dry_proc.stdout + dry_proc.stderr
    assert secret_dtb not in dry_proc.stdout + dry_proc.stderr
    parsed = json.loads(dry_proc.stdout)
    assert parsed["dry_run"] is True
    assert parsed["phases"] == [5, 6, 7, 8, 9]
    assert len(parsed["checks"]) >= 8
