"""The source repository must remain recoverable across isolated instance pull publications."""

import pytest

from semantica_dataagent import source_directory


def test_pull_publication_is_one_commit_and_rejects_staged_changes(tmp_path):
    with source_directory.repository(tmp_path / "source") as root:
        (root / "prior.txt").write_text("prior", encoding="utf-8")
        source_directory.git(root, "add", "prior.txt")
        source_directory.git(root, "commit", "-m", "initial", author="tester")
        before = source_directory.git(root, "rev-parse", "HEAD")

        commit, changed = source_directory.publish_pull_instance(
            root,
            "inst-1",
            {"model.yaml": b"name: m\n"},
            tasks=[{"name": "t1", "sql": "SELECT 1"}],
            dimension_values=[
                {"database": "d", "table": "t", "field": "f", "value": "v"}
            ],
            operation_id="op-pub-1",
        )
        assert changed is True
        assert commit is not None and commit.encode() != before.strip()
        assert (
            source_directory.git(
                root, "show", "--format=%an", "--no-patch", commit
            ).strip()
            == "定时拉取".encode()
        )
        assert (
            source_directory.git(root, "show", f"{commit}:ossie/inst-1/model.yaml")
            == b"name: m\n"
        )
        assert (root / "pulls" / "inst-1" / "tasks.json").is_file()
        assert (root / "pulls" / "inst-1" / "dimension_values.json").is_file()

        (root / "prior.txt").write_text("staged", encoding="utf-8")
        source_directory.git(root, "add", "prior.txt")
        with pytest.raises(ValueError, match="staged"):
            source_directory.publish_pull_instance(
                root, "inst-1", {"model.yaml": b"name: new\n"}
            )
        assert (
            source_directory.git(root, "rev-parse", "HEAD").decode().strip() == commit
        )
        assert (
            source_directory.git(root, "show", f"{commit}:ossie/inst-1/model.yaml")
            == b"name: m\n"
        )


def test_cleanup_pull_instance_files_preserves_other_files(tmp_path):
    with source_directory.repository(tmp_path / "source") as root:
        (root / "ossie" / "manual.yaml").parent.mkdir(parents=True, exist_ok=True)
        (root / "ossie" / "manual.yaml").write_text("name: manual\n", encoding="utf-8")
        source_directory.git(root, "add", "ossie/manual.yaml")
        source_directory.git(root, "commit", "-m", "manual", author="tester")

        source_directory.publish_pull_instance(
            root,
            "inst-a",
            {"m.yaml": b"name: a\n"},
            tasks=[{"name": "ta", "sql": "SELECT 1"}],
        )
        source_directory.publish_pull_instance(
            root,
            "inst-b",
            {"m.yaml": b"name: b\n"},
            tasks=[{"name": "tb", "sql": "SELECT 1"}],
        )

        commit = source_directory.cleanup_pull_instance_files(root, "inst-a")
        assert commit is not None
        assert not (root / "ossie" / "inst-a").exists()
        assert not (root / "pulls" / "inst-a").exists()
        assert (root / "ossie" / "manual.yaml").is_file()
        assert (root / "ossie" / "inst-b" / "m.yaml").is_file()
        assert (root / "pulls" / "inst-b" / "tasks.json").is_file()
