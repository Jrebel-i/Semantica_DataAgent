"""Guard Kubernetes deployment manifests, Dockerfile build inputs, and Alembic upgrades."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_MANIFEST = REPO_ROOT / "deploy" / "k8s" / "app" / "application.yaml"
DOCKERFILE = REPO_ROOT / "deploy" / "k8s" / "app" / "Dockerfile"
MIGRATIONS_DIR = REPO_ROOT / "backend" / "src" / "semantica_dataagent" / "migrations"

PHASE_5_9_TABLES = {
    "interaction_trajectories",
    "evolution_runs",
    "evolution_rounds",
    "evolution_checkpoints",
    "discovery_runs",
    "discovery_candidates",
    "personal_dtb_tokens",
}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _sqlite_tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


def test_k8s_application_manifest_preserves_single_replica_and_security() -> None:
    docs = list(yaml.safe_load_all(APP_MANIFEST.read_text(encoding="utf-8")))
    deployment = next(doc for doc in docs if doc and doc.get("kind") == "Deployment")

    spec = deployment["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"

    pod_spec = spec["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True

    container = pod_spec["containers"][0]
    sec = container["securityContext"]
    assert sec["allowPrivilegeEscalation"] is False
    assert "ALL" in sec["capabilities"]["drop"]

    env_names = {item["name"] for item in container["env"]}
    assert {
        "DATAAGENT_DB_PATH",
        "DATAAGENT_ENCRYPTION_KEY",
        "DATAAGENT_AGENT_MODE",
        "DATAAGENT_PUBLIC_URL",
    } <= env_names


def test_app_dockerfile_bundles_schemas_skills_backend_and_static_dist() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY schemas ./schemas" in text
    assert "COPY skills ./skills" in text
    assert "COPY backend/src ./backend/src" in text
    assert "RUN pnpm build" in text
    assert "COPY --from=frontend /app/web/dist /app/static" in text


def test_alembic_single_head_and_upgrade_from_phase4_0037_to_head(
    tmp_path: Path,
) -> None:
    probe_cfg = _alembic_config(tmp_path / "probe.db")
    heads = ScriptDirectory.from_config(probe_cfg).get_heads()
    assert len(heads) == 1, f"Expected single Alembic head, got {heads}"

    fresh_db = tmp_path / "fresh.db"
    command.upgrade(_alembic_config(fresh_db), "head")
    assert PHASE_5_9_TABLES <= _sqlite_tables(fresh_db)

    upgrade_db = tmp_path / "phase4_to_head.db"
    upgrade_cfg = _alembic_config(upgrade_db)
    command.upgrade(upgrade_cfg, "0037")
    before_tables = _sqlite_tables(upgrade_db)
    assert "discovery_runs" not in before_tables
    assert "evolution_runs" not in before_tables

    command.upgrade(upgrade_cfg, "head")
    after_tables = _sqlite_tables(upgrade_db)
    assert PHASE_5_9_TABLES <= after_tables
