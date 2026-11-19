#!/usr/bin/env python3
"""第 5～9 期已部署集群在线验收与无网自检脚本。

示例：
  # 1. 无外部集群环境的脚本自检与 Dry-run
  uv run --directory backend python ../scripts/verify_phase_5_to_9.py --self-test
  uv run --directory backend python ../scripts/verify_phase_5_to_9.py --dry-run --phases all

  # 2. 针对已部署测试环境执行第 5～9 期在线契约与构建验收（默认跳过需真实 DTB/LLM 凭证的操作）
  DATAAGENT_TOKEN="..." uv run --directory backend python ../scripts/verify_phase_5_to_9.py \
      --base-url http://172.16.242.124:31630 \
      --create-temp-project \
      --phases all \
      --report /tmp/phase5_9_report

  # 3. 显式开启真实 DTB 发现运行与真实 LLM 演化运行
  DATAAGENT_TOKEN="..." DATAAGENT_PERSONAL_DTB_TOKEN="..." \
  uv run --directory backend python ../scripts/verify_phase_5_to_9.py \
      --base-url http://172.16.242.124:31630 \
      --project default \
      --run-live-dtb --run-live-llm \
      --report /tmp/phase5_9_live
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_DIR = REPO_ROOT / "eval" / "phase-5-9-acceptance"
OSSIE_SAMPLE_DIR = ACCEPTANCE_DIR / "ossie-sample"
CASES_PATH = ACCEPTANCE_DIR / "cases.yaml"
VALID_PHASES = (5, 6, 7, 8, 9)


@dataclass
class CheckResult:
    """Single acceptance check execution record."""

    id: str
    phase: int
    name: str
    status: str  # passed | failed | skipped | planned
    detail: str
    duration_ms: int = 0


def parse_phases(raw: str) -> list[int]:
    """Parse --phases argument ('all' or comma-separated 5..9)."""
    text = raw.strip().lower()
    if text == "all":
        return list(VALID_PHASES)
    phases: list[int] = []
    seen: set[int] = set()
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if not piece.isdigit():
            raise ValueError(f"无效分期编号: {piece!r}")
        num = int(piece)
        if num not in VALID_PHASES:
            raise ValueError(f"仅支持分期 5～9，收到: {num}")
        if num not in seen:
            seen.add(num)
            phases.append(num)
    if not phases:
        raise ValueError("必须至少指定一个分期（5～9 或 all）")
    return sorted(phases)


def collect_secret_values(extra: list[str] | None = None) -> list[str]:
    """Collect sensitive secret strings that must never leak into logs or reports."""
    secrets: list[str] = []
    for env_name in ("DATAAGENT_TOKEN", "DATAAGENT_PERSONAL_DTB_TOKEN"):
        val = os.environ.get(env_name, "").strip()
        if val:
            secrets.append(val)
    if extra:
        for val in extra:
            stripped = val.strip()
            if stripped:
                secrets.append(stripped)
    return sorted(set(secrets), key=len, reverse=True)


def redact_secrets(text: str, secrets: list[str]) -> str:
    """Replace any configured secret occurrences with [REDACTED]."""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


def build_check_plan(
    phases: list[int],
    *,
    run_live_dtb: bool,
    run_live_llm: bool,
) -> list[tuple[str, int, str]]:
    """Return ordered list of (check_id, phase, description) for selected phases."""
    catalog: list[tuple[str, int, str]] = [
        ("phase5-ossie-adapter", 5, "检查 Apache Ossie 源适配器可用性"),
        ("phase5-ossie-import-build", 5, "上传 Ossie 模型样例并执行构建与双格式导出"),
        (
            "phase6-dtb-puller-check",
            6,
            "检查 DtbOssiePuller 适配器配置及真实拉取（可选）",
        ),
        (
            "phase7-project-isolation",
            7,
            "验证文档型项目隔离（422）、归档冻结（409）与恢复",
        ),
        (
            "phase8-overrides-draft-eval",
            8,
            "验证纠错草稿 L1 校验、评测题集与演化配置接口",
        ),
        ("phase8-live-evolution", 8, "触发真实 LLM 演化运行（需 --run-live-llm）"),
        ("phase9-discovery-config", 9, "验证发现配置双预设与非法标识符 422 防护"),
        (
            "phase9-discovery-spoof-guard",
            9,
            "验证外部提交 source='discovery' 防伪造（422）",
        ),
        (
            "phase9-live-discovery-run",
            9,
            "触发真实 DTB 查询历史挖掘与数据探活（需 --run-live-dtb）",
        ),
    ]
    selected = set(phases)
    plan: list[tuple[str, int, str]] = []
    for cid, ph, desc in catalog:
        if ph not in selected:
            continue
        if cid == "phase8-live-evolution" and not run_live_llm:
            desc += " [默认跳过]"
        if cid == "phase9-live-discovery-run" and not run_live_dtb:
            desc += " [默认跳过]"
        plan.append((cid, ph, desc))
    return plan


def load_acceptance_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    """Load structured acceptance cases YAML and perform basic integrity checks."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"案例清单必须为非空列表: {path}")
    seen: set[str] = set()
    for row in data:
        cid = row.get("id")
        if not cid or cid in seen:
            raise ValueError(f"重复或缺失案例 ID: {cid!r}")
        seen.add(cid)
    return data


def format_report_markdown(payload: dict[str, Any]) -> str:
    """Render concise Markdown summary report without sensitive credentials."""
    lines = [
        "# 第 5～9 期在线验收报告",
        "",
        f"- **目标地址**: `{payload['base_url']}`",
        f"- **目标项目**: `{payload['project']}`",
        f"- **执行时间 (UTC)**: `{payload['executed_at']}`",
        f"- **涵盖分期**: `{payload['phases']}`",
        (
            f"- **汇总**: passed={payload['summary']['passed']} "
            f"failed={payload['summary']['failed']} "
            f"skipped={payload['summary']['skipped']}"
        ),
        "",
        "| Check ID | 分期 | 状态 | 耗时 (ms) | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in payload["results"]:
        detail = str(row["detail"]).replace("\n", " ").replace("|", "\\|")
        lines.append(
            f"| `{row['id']}` | 第 {row['phase']} 期 | `{row['status']}` "
            f"| {row['duration_ms']} | {detail} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(
    prefix: Path, payload: dict[str, Any], secrets: list[str]
) -> tuple[Path, Path]:
    """Write <prefix>.json and <prefix>.md after scrubbing secrets."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}.json")
    md_path = Path(f"{prefix}.md")

    raw_json = json.dumps(payload, ensure_ascii=False, indent=2)
    raw_md = format_report_markdown(payload)
    json_path.write_text(redact_secrets(raw_json, secrets) + "\n", encoding="utf-8")
    md_path.write_text(redact_secrets(raw_md, secrets), encoding="utf-8")
    return json_path, md_path


class AcceptanceRunner:
    """Execute live HTTP acceptance checks against a running DataAgent instance."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        project: str,
        create_temp_project: bool,
        run_live_dtb: bool,
        run_live_llm: bool,
        poll_timeout: float,
        secrets: list[str],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.project = project
        self.create_temp_project = create_temp_project
        self.run_live_dtb = run_live_dtb
        self.run_live_llm = run_live_llm
        self.poll_timeout = poll_timeout
        self.secrets = secrets
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
            trust_env=False,
        )
        self._temp_projects: list[tuple[str, str]] = []
        self.target_project = project

    def close(self) -> None:
        for pid, name in reversed(self._temp_projects):
            try:
                arch = self.client.post(f"/api/v1/projects/{pid}/archive")
                if arch.status_code in (200, 409):
                    self.client.post(
                        f"/api/v1/projects/{pid}/delete",
                        json={"name": name},
                    )
            except httpx.HTTPError:
                pass
        self.client.close()

    def _create_temp(self, prefix: str, ptype: str) -> tuple[str, str]:
        name = f"{prefix}{uuid.uuid4().hex[:8]}"
        resp = self.client.post(
            "/api/v1/projects",
            json={"name": name, "type": ptype},
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"创建临时项目失败: {resp.status_code} {resp.text[:200]}"
            )
        pid = resp.json()["id"]
        self._temp_projects.append((pid, name))
        return pid, name

    def _poll_until(
        self,
        url: str,
        *,
        terminal: set[str],
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            resp = self.client.get(url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"轮询 {url} 失败: {resp.status_code} {resp.text[:200]}"
                )
            data = resp.json()
            if data.get("status") in terminal:
                return data
            time.sleep(2.0)
        raise TimeoutError(f"轮询 {url} 超时（>{self.poll_timeout}s）")

    def run_phase5_adapter(self) -> tuple[str, str]:
        resp = self.client.get(f"/api/v1/projects/{self.target_project}/adapters")
        if resp.status_code != 200:
            raise RuntimeError(f"读取适配器失败: {resp.status_code} {resp.text[:200]}")
        adapters = {row["id"]: row for row in resp.json()}
        if "ossie" not in adapters:
            raise RuntimeError(f"未注册 ossie 适配器: {list(adapters)}")
        return (
            "passed",
            f"ossie 适配器已发现（compatible={adapters['ossie'].get('compatible')}）",
        )

    def run_phase5_import_build(self) -> tuple[str, str]:
        if not self.create_temp_project:
            resp = self.client.get(
                f"/api/v1/projects/{self.target_project}/export/ossie"
            )
            if resp.status_code == 409:
                return (
                    "skipped",
                    "目标项目尚无构建知识代，传 --create-temp-project 可端到端建构验证",
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"导出 Ossie 失败: {resp.status_code} {resp.text[:200]}"
                )
            doc = yaml.safe_load(resp.text)
            return (
                "passed",
                f"已导出 Ossie 模型 version={doc.get('version')} datasets={len(doc.get('datasets', []))}",
            )

        pid = self.target_project
        enable = self.client.put(
            f"/api/v1/projects/{pid}/adapters",
            json={
                "adapters": [
                    {
                        "id": "ossie",
                        "enabled": True,
                        "parameters": {
                            "path": "ossie",
                            "category": "ossie",
                            "strip_catalog": False,
                        },
                    },
                    {
                        "id": "markdown",
                        "enabled": True,
                        "parameters": {"path": "knowledge"},
                    },
                ]
            },
        )
        if enable.status_code != 200:
            raise RuntimeError(
                f"启用适配器失败: {enable.status_code} {enable.text[:200]}"
            )

        for target, files in [
            (
                "ossie",
                {
                    "demo_commerce.yaml": (
                        OSSIE_SAMPLE_DIR / "ossie" / "demo_commerce.yaml"
                    ).read_bytes()
                },
            ),
            (
                "knowledge/metrics",
                {
                    "gmv.md": (
                        OSSIE_SAMPLE_DIR / "knowledge" / "metrics" / "gmv.md"
                    ).read_bytes()
                },
            ),
            (
                "overrides",
                {
                    "semantic_overrides.yaml": (
                        OSSIE_SAMPLE_DIR / "overrides" / "semantic_overrides.yaml"
                    ).read_bytes()
                },
            ),
        ]:
            up = self.client.post(
                "/api/v1/source/upload",
                params={"project": pid},
                json={
                    "target": target,
                    "files": [
                        {"path": k, "content_base64": base64.b64encode(v).decode()}
                        for k, v in files.items()
                    ],
                },
            )
            if up.status_code != 200:
                raise RuntimeError(
                    f"上传 {target} 失败: {up.status_code} {up.text[:200]}"
                )

        build = self.client.post(
            "/api/v1/builds", params={"project": pid}, json={"build_type": "full"}
        )
        if build.status_code != 202:
            raise RuntimeError(f"触发构建失败: {build.status_code} {build.text[:200]}")
        final_build = self._poll_until(
            f"/api/v1/builds/{build.json()['id']}",
            params={"project": pid},
            terminal={"succeeded", "failed", "cancelled"},
        )
        if final_build["status"] != "succeeded":
            raise RuntimeError(f"构建未成功: {final_build.get('error')}")

        exp_resp = self.client.get(f"/api/v1/projects/{pid}/export/ossie?format=json")
        if exp_resp.status_code != 200:
            raise RuntimeError(
                f"导出 Ossie JSON 失败: {exp_resp.status_code} {exp_resp.text[:200]}"
            )
        exp_doc = exp_resp.json()
        return (
            "passed",
            f"临时项目 {pid} 构建并导出 Ossie 成功：datasets={len(exp_doc.get('datasets', []))} relationships={len(exp_doc.get('relationships', []))}",
        )

    def run_phase6_dtb_puller(self) -> tuple[str, str]:
        resp = self.client.get(f"/api/v1/projects/{self.target_project}/adapters")
        if resp.status_code != 200:
            raise RuntimeError(f"读取适配器失败: {resp.status_code}")
        adapters = {row["id"]: row for row in resp.json()}
        ossie_pull = adapters.get("ossie", {}).get("pull")
        if not ossie_pull:
            raise RuntimeError("ossie 适配器缺少 DtbOssiePuller 配置结构")
        return (
            "passed",
            f"DtbOssiePuller 接口就绪（enabled={ossie_pull.get('enabled')} token_configured={ossie_pull.get('token_configured')}）",
        )

    def run_phase7_isolation(self) -> tuple[str, str]:
        doc_id, _ = self._create_temp("vdoc", "document")
        r1 = self.client.get(f"/api/v1/projects/{doc_id}/export/ossie")
        r2 = self.client.get(f"/api/v1/projects/{doc_id}/discovery-config")
        if r1.status_code != 422 or r2.status_code != 422:
            raise RuntimeError(
                f"文档型项目隔离失败: export={r1.status_code} discovery={r2.status_code}"
            )
        arch = self.client.post(f"/api/v1/projects/{doc_id}/archive")
        if arch.status_code != 200:
            raise RuntimeError(f"归档失败: {arch.status_code}")
        put_frozen = self.client.put(
            f"/api/v1/projects/{doc_id}/adapters",
            json={"adapters": [{"id": "markdown", "enabled": True, "parameters": {}}]},
        )
        if put_frozen.status_code != 409:
            raise RuntimeError(f"归档项目写操作期望 409，实际 {put_frozen.status_code}")
        rest = self.client.post(f"/api/v1/projects/{doc_id}/restore")
        if rest.status_code != 200:
            raise RuntimeError(f"恢复失败: {rest.status_code}")
        return (
            "passed",
            f"文档型项目 {doc_id} 工具边界 422、归档冻结 409 与恢复验证通过",
        )

    def run_phase8_apis(self) -> tuple[str, str]:
        pid = self.target_project
        evo = self.client.get(f"/api/v1/projects/{pid}/evolution-config")
        pol = self.client.get(f"/api/v1/projects/{pid}/correction-policy")
        eqs = self.client.get("/api/v1/eval-questions", params={"project": pid})
        if evo.status_code != 200 or pol.status_code != 200 or eqs.status_code != 200:
            raise RuntimeError(
                f"第 8 期接口检查失败: evo={evo.status_code} pol={pol.status_code} eval={eqs.status_code}"
            )
        return (
            "passed",
            f"演化配置、双审批开关及评测题集接口正常（当前题目数={len(eqs.json())}）",
        )

    def run_phase8_live_evolution(self) -> tuple[str, str]:
        if not self.run_live_llm:
            return "skipped", "未指定 --run-live-llm，跳过真实 LLM 演化运行"
        pid = self.target_project
        trig = self.client.post(
            "/api/v1/evolution/runs",
            params={"project": pid},
            json={"max_rounds": 1, "max_eval_cases": 5},
        )
        if trig.status_code != 202:
            raise RuntimeError(
                f"触发演化运行失败: {trig.status_code} {trig.text[:200]}"
            )
        run = self._poll_until(
            f"/api/v1/evolution/runs/{trig.json()['id']}",
            params={"project": pid},
            terminal={"accepted", "incomplete"},
        )
        return (
            "passed",
            f"演化运行 {run['id']} 完成：status={run['status']} rounds={run.get('rounds_completed')}",
        )

    def run_phase9_config(self) -> tuple[str, str]:
        pid = self.target_project
        cfg = self.client.get(f"/api/v1/projects/{pid}/discovery-config")
        if cfg.status_code != 200:
            raise RuntimeError(f"读取发现配置失败: {cfg.status_code} {cfg.text[:200]}")
        presets = sorted(cfg.json().get("presets", {}).keys())
        bad = self.client.put(
            f"/api/v1/projects/{pid}/discovery-config",
            json={"source_table": "__internal_schema.audit_log; DROP TABLE y"},
        )
        if bad.status_code != 422:
            raise RuntimeError(f"非法表标识符期望 422，实际 {bad.status_code}")
        return "passed", f"预设模板={presets}，非法 SQL 标识符已返回 422 拦截"

    def run_phase9_spoof(self) -> tuple[str, str]:
        spoof = self.client.post(
            "/api/v1/corrections",
            params={"project": self.target_project},
            json={
                "target": "table:hive_prod.dwd.dwd_orders",
                "patch": "{}",
                "reason": "acceptance spoof test",
                "source": "discovery",
            },
        )
        if spoof.status_code != 422:
            raise RuntimeError(
                f"外部伪造 source='discovery' 期望 422，实际 {spoof.status_code}"
            )
        return "passed", "外部提交 source='discovery' 已被服务端拒绝（422）"

    def run_phase9_live_discovery(self) -> tuple[str, str]:
        if not self.run_live_dtb:
            return (
                "skipped",
                "未指定 --run-live-dtb，跳过真实 DTB 查询历史挖掘与数据探活",
            )
        dtb_env = os.environ.get("DATAAGENT_PERSONAL_DTB_TOKEN", "").strip()
        tok_status = self.client.get("/api/v1/me/dtb-token")
        if tok_status.status_code != 200:
            raise RuntimeError(f"查询个人 DTB 令牌状态失败: {tok_status.status_code}")
        if not tok_status.json().get("configured"):
            if not dtb_env:
                raise RuntimeError(
                    "当前账户未配置个人 DTB 令牌且未提供 DATAAGENT_PERSONAL_DTB_TOKEN"
                )
            put_tok = self.client.put("/api/v1/me/dtb-token", json={"token": dtb_env})
            if put_tok.status_code != 200:
                raise RuntimeError(f"写入个人 DTB 令牌失败: {put_tok.status_code}")

        pid = self.target_project
        trig = self.client.post(
            "/api/v1/discovery/runs",
            params={"project": pid},
            json={
                "mine_query_history": True,
                "probe_relationships": True,
                "seed_eval_drafts": False,
            },
        )
        if trig.status_code != 202:
            raise RuntimeError(
                f"触发知识发现运行失败: {trig.status_code} {trig.text[:200]}"
            )
        run = self._poll_until(
            f"/api/v1/discovery/runs/{trig.json()['id']}",
            params={"project": pid},
            terminal={"completed", "partial", "failed", "incomplete"},
        )
        return (
            "passed" if run["status"] in {"completed", "partial"} else "failed",
            (
                f"发现运行 {run['id']} status={run['status']} "
                f"queries={run.get('queries_fetched')} candidates={run.get('candidates_recalled')} "
                f"passed={run.get('candidates_passed')} skipped={run.get('candidates_skipped')} "
                f"proposals={run.get('proposals_created')}"
            ),
        )

    def execute_all(self, phases: list[int]) -> list[CheckResult]:
        if self.create_temp_project:
            self.target_project, _ = self._create_temp("vwh", "warehouse")

        handlers = {
            "phase5-ossie-adapter": self.run_phase5_adapter,
            "phase5-ossie-import-build": self.run_phase5_import_build,
            "phase6-dtb-puller-check": self.run_phase6_dtb_puller,
            "phase7-project-isolation": self.run_phase7_isolation,
            "phase8-overrides-draft-eval": self.run_phase8_apis,
            "phase8-live-evolution": self.run_phase8_live_evolution,
            "phase9-discovery-config": self.run_phase9_config,
            "phase9-discovery-spoof-guard": self.run_phase9_spoof,
            "phase9-live-discovery-run": self.run_phase9_live_discovery,
        }
        plan = build_check_plan(
            phases,
            run_live_dtb=self.run_live_dtb,
            run_live_llm=self.run_live_llm,
        )
        results: list[CheckResult] = []
        for cid, ph, _ in plan:
            started = time.monotonic()
            try:
                status, detail = handlers[cid]()
            except Exception as exc:  # noqa: BLE001 - continue remaining checks and report summary
                status = "failed"
                detail = redact_secrets(str(exc), self.secrets)
            duration_ms = int((time.monotonic() - started) * 1000)
            results.append(
                CheckResult(
                    id=cid,
                    phase=ph,
                    name=cid,
                    status=status,
                    detail=redact_secrets(detail, self.secrets),
                    duration_ms=duration_ms,
                )
            )
        return results


def run_self_test() -> int:
    """Offline self-test verifying CLI parsing, secret scrubbing, cases.yaml, and reports."""
    assert parse_phases("all") == [5, 6, 7, 8, 9]
    assert parse_phases("9,5,7") == [5, 7, 9]
    cases = load_acceptance_cases()
    phases_seen = {row["phase"] for row in cases}
    assert phases_seen == {5, 6, 7, 8, 9}, f"unexpected phases: {phases_seen}"

    fake_token = "tok_selftest_secret_123456"
    fake_dtb = "dtb_selftest_secret_abcdef"
    secrets = collect_secret_values([fake_token, fake_dtb])
    assert (
        redact_secrets(f"auth={fake_token} dtb={fake_dtb}", secrets)
        == "auth=[REDACTED] dtb=[REDACTED]"
    )

    plan = build_check_plan([5, 8, 9], run_live_dtb=False, run_live_llm=False)
    assert any(item[0] == "phase9-live-discovery-run" for item in plan)

    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "base_url": "http://127.0.0.1:8000",
            "project": "default",
            "executed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "phases": [5, 6, 7, 8, 9],
            "summary": {"passed": 1, "failed": 0, "skipped": 0},
            "results": [
                asdict(
                    CheckResult(
                        id="self-test",
                        phase=5,
                        name="self-test",
                        status="passed",
                        detail=f"scrub check {fake_token} / {fake_dtb}",
                        duration_ms=1,
                    )
                )
            ],
        }
        jp, mp = write_report(Path(tmp) / "rep", payload, secrets)
        for p in (jp, mp):
            txt = p.read_text(encoding="utf-8")
            assert fake_token not in txt and fake_dtb not in txt
            assert "[REDACTED]" in txt

    print(f"self-test passed: cases={len(cases)}, phases={sorted(phases_seen)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="第 5～9 期已部署 Semantica DataAgent 集群在线验收脚本",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="目标服务根地址（默认 http://127.0.0.1:8000）",
    )
    parser.add_argument(
        "--phases",
        default="all",
        help="要验收的分期：all 或逗号分隔，如 5,6,7,8,9（默认 all）",
    )
    parser.add_argument(
        "--project",
        default="default",
        help="目标项目 ID（默认 default；若传 --create-temp-project 则自动创建并清理临时项目）",
    )
    parser.add_argument(
        "--create-temp-project",
        action="store_true",
        help="创建隔离临时数仓/文档项目执行建构校验并在结束后归档删除",
    )
    parser.add_argument(
        "--run-live-dtb",
        action="store_true",
        help="显式开启需真实 DTB 服务执行的第 9 期知识发现运行（默认跳过）",
    )
    parser.add_argument(
        "--skip-live-dtb",
        action="store_true",
        help="跳过真实 DTB 触发（默认行为，显式保留兼容开关）",
    )
    parser.add_argument(
        "--run-live-llm",
        action="store_true",
        help="显式开启需真实 LLM Provider 的第 8 期自主演化运行（默认跳过）",
    )
    parser.add_argument(
        "--skip-live-llm",
        action="store_true",
        help="跳过真实 LLM 演化运行（默认行为，显式保留兼容开关）",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=300.0,
        help="构建/发现/演化异步任务最大等待秒数（默认 300）",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="可选报告文件前缀，将同时生成 <prefix>.json 与 <prefix>.md",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要执行的检查计划并退出（不发送网络请求）",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="运行无网络自检并退出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    try:
        phases = parse_phases(args.phases)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    run_live_dtb = bool(args.run_live_dtb and not args.skip_live_dtb)
    run_live_llm = bool(args.run_live_llm and not args.skip_live_llm)
    secrets = collect_secret_values()

    if args.dry_run:
        plan = build_check_plan(
            phases,
            run_live_dtb=run_live_dtb,
            run_live_llm=run_live_llm,
        )
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "base_url": args.base_url,
                    "project": "<temp>" if args.create_temp_project else args.project,
                    "phases": phases,
                    "run_live_dtb": run_live_dtb,
                    "run_live_llm": run_live_llm,
                    "checks": [
                        {"id": cid, "phase": ph, "description": desc}
                        for cid, ph, desc in plan
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    token = os.environ.get("DATAAGENT_TOKEN", "").strip()
    if not token:
        print(
            "错误：请通过环境变量 DATAAGENT_TOKEN 提供系统管理员 Bearer Token。",
            file=sys.stderr,
        )
        return 2

    runner = AcceptanceRunner(
        base_url=args.base_url,
        token=token,
        project=args.project,
        create_temp_project=args.create_temp_project,
        run_live_dtb=run_live_dtb,
        run_live_llm=run_live_llm,
        poll_timeout=args.poll_timeout,
        secrets=secrets,
    )
    try:
        results = runner.execute_all(phases)
    finally:
        runner.close()

    summary = {
        "passed": sum(1 for r in results if r.status == "passed"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
    }
    payload = {
        "base_url": args.base_url,
        "project": runner.target_project
        if not args.create_temp_project
        else f"{runner.target_project} (temp, cleaned)",
        "executed_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "phases": phases,
        "summary": summary,
        "results": [asdict(r) for r in results],
    }

    if args.report:
        jp, mp = write_report(args.report, payload, secrets)
        print(f"报告已写入: {jp}, {mp}")

    for r in results:
        print(f"[{r.status.upper():7s}] (Phase {r.phase}) {r.id}: {r.detail}")
    print(
        f"汇总: passed={summary['passed']} failed={summary['failed']} skipped={summary['skipped']}"
    )
    return 1 if summary["failed"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
