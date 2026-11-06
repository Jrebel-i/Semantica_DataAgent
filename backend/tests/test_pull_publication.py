"""The source repository must remain recoverable across a pull publication."""

import pytest

from semantica_dataagent import source_directory


def test_pull_publication_is_one_commit_and_rejects_staged_changes(tmp_path):
    with source_directory.repository(tmp_path / "source") as root:
        (root / "prior.txt").write_text("prior")
        source_directory.git(root, "add", "prior.txt")
        source_directory.git(root, "commit", "-m", "initial", author="tester")
        before = source_directory.git(root, "rev-parse", "HEAD")
        publish = getattr(source_directory, "publish_pull", None)
        assert publish is not None, "pull publication must be implemented"
        commit = publish(root, {"任务信息.json": b"tasks", "表信息.xlsx": b"tables"})
        assert commit.encode() != before.strip()
        assert (
            source_directory.git(
                root, "show", "--format=%an", "--no-patch", commit
            ).strip()
            == "定时拉取".encode()
        )
        snapshot = next((root / "dtb").iterdir()).name
        assert (
            source_directory.git(root, "show", f"{commit}:dtb/{snapshot}/任务信息.json")
            == b"tasks"
        )
        (root / "prior.txt").write_text("staged")
        source_directory.git(root, "add", "prior.txt")
        with pytest.raises(ValueError, match="staged"):
            publish(root, {"任务信息.json": b"new"})
        assert (
            source_directory.git(root, "rev-parse", "HEAD").decode().strip() == commit
        )
        assert (
            source_directory.git(root, "show", f"{commit}:dtb/{snapshot}/任务信息.json")
            == b"tasks"
        )


@pytest.mark.parametrize("point", ["rename", "add", "commit"])
def test_interrupted_pull_recovers_without_duplicate_commit(tmp_path, point):
    import os
    import subprocess
    import sys

    path = tmp_path / "source"
    with source_directory.repository(path) as root:
        (root / "prior.txt").write_text("prior")
        source_directory.git(root, "add", "prior.txt")
        source_directory.git(root, "commit", "-m", "initial", author="tester")
        before = source_directory.git(root, "rev-parse", "HEAD").decode().strip()
    script = """
import os, sys, signal
from semantica_dataagent import source_directory as source
point = sys.argv[2]
original_git = source.git
original_replace = source.os.replace

def git(root, *args, **kwargs):
    result = original_git(root, *args, **kwargs)
    if args[0] == point:
        os.kill(os.getpid(), signal.SIGKILL)
    return result

def replace(src, dst):
    original_replace(src, dst)
    if point == "rename" and ".pull-" in str(src):
        os.kill(os.getpid(), signal.SIGKILL)
source.git = git
source.os.replace = replace
with source.repository(sys.argv[1]) as root:
    source.publish_pull(root, {"任务信息.json": b"tasks", "表信息.xlsx": b"tables"})
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(path), point],
        env=os.environ.copy(),
        check=False,
    )
    assert child.returncode == -9
    with source_directory.repository(path) as root:
        head = source_directory.git(root, "rev-parse", "HEAD").decode().strip()
        if point == "commit":
            assert head != before
            assert (
                source_directory.git(root, "rev-list", "--count", "HEAD").strip()
                == b"2"
            )
        else:
            assert head == before
            assert not list((root / "dtb").iterdir())
        assert not source_directory.git(root, "diff", "--cached", "--name-only")
        assert not (root / ".git" / "dataagent-pull.json").exists()
