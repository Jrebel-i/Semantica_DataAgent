import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_agents_size.py"


def run_check(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=directory, capture_output=True, text=True
    )


def test_agents_size_limit(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(b"a" * 16000)
    assert run_check(tmp_path).returncode == 0
    agents.write_bytes(b"a" * 16001)
    assert run_check(tmp_path).returncode != 0


def test_repository_agents_size() -> None:
    assert run_check(ROOT).returncode == 0
