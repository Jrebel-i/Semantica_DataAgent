import subprocess
from pathlib import Path


def test_business_data_is_ignored():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "data/example.csv", "data/nested/example.csv"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == {
        "data/example.csv",
        "data/nested/example.csv",
    }
