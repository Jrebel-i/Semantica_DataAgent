"""Security contract for Kubernetes agent sessions and namespace permissions."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def pod_spec():
    module = importlib.util.find_spec("semantica_dataagent.k8s_sandbox")
    assert module is not None, "Kubernetes session sandbox must be implemented"
    from unittest.mock import patch

    from semantica_dataagent.k8s_sandbox import build_pod_spec

    def resolve(host, *args, **kwargs):
        ips = {
            "gateway.example": "203.0.113.10",
            "dataagent.semantica-dataagent.svc": "10.96.0.25",
        }
        return [(2, 1, 6, "", (ips[host], 0))]

    with patch("socket.getaddrinfo", side_effect=resolve):
        return build_pod_spec(
            name="sda-agent-example",
            workspace_subpath="conversations/example",
            model_url="https://gateway.example:443/v1",
            mcp_url="http://dataagent.semantica-dataagent.svc:8000/mcp/",
        )


def manifests():
    path = ROOT / "deploy/k8s/app"
    assert path.is_dir(), "application Kubernetes manifests must exist"
    return [
        doc
        for file in path.glob("*.yaml")
        for doc in yaml.safe_load_all(file.read_text())
        if isinstance(doc, dict)
    ]


def test_session_pod_is_labeled_and_limits_its_workspace():
    pod = pod_spec()
    assert pod["metadata"]["labels"]["app.kubernetes.io/component"] == "agent-session"
    spec = pod["spec"]
    assert spec.get("hostNetwork", False) is False
    assert spec.get("automountServiceAccountToken", True) is False
    container = spec["containers"][0]
    security = container["securityContext"]
    assert security["runAsNonRoot"] is True
    assert security["readOnlyRootFilesystem"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert "ALL" in security["capabilities"]["drop"]
    assert not security["capabilities"].get("add")
    mounts = [m for m in container["volumeMounts"] if m.get("subPath")]
    assert not mounts
    assert all(volume.get("emptyDir") == {} for volume in spec["volumes"])


def test_session_firewall_denies_ipv4_and_ipv6_by_default():
    pod = pod_spec()
    init = pod["spec"]["initContainers"][0]
    assert "NET_ADMIN" in init["securityContext"]["capabilities"]["add"]
    script = "\n".join(init.get("command", []) + init.get("args", []))
    for command in ("iptables", "ip6tables"):
        assert command in script
        assert f"{command} -P OUTPUT DROP" in script
    assert "203.0.113.10" in script
    assert "10.96.0.25" in script


def test_application_rbac_is_namespace_scoped_and_minimal():
    docs = manifests()
    roles = [d for d in docs if d.get("kind") == "Role"]
    assert roles, "application Role is required"
    assert not any(d.get("kind") in ("ClusterRole", "ClusterRoleBinding") for d in docs)
    for role in roles:
        assert role["metadata"]["namespace"] == "semantica-dataagent-sessions"
        for rule in role["rules"]:
            assert set(rule["resources"]) <= {"pods", "pods/log", "pods/exec"}
            assert set(rule["verbs"]) <= {"get", "list", "watch", "create", "delete"}
            assert rule["apiGroups"] == [""]


def test_network_policy_selects_sessions_and_restricts_egress():
    policies = [d for d in manifests() if d.get("kind") == "NetworkPolicy"]
    assert policies, "session NetworkPolicy is required"
    policy = next(
        p
        for p in policies
        if p["spec"]["podSelector"]
        .get("matchLabels", {})
        .get("app.kubernetes.io/component")
        == "agent-session"
    )
    assert "Egress" in policy["spec"]["policyTypes"]
    for rule in policy["spec"].get("egress", []):
        assert rule.get("to"), "unrestricted egress destination"
        assert rule.get("ports"), "unrestricted egress ports"


def test_exec_wrapper_passes_environment_and_stream_without_command_line_secrets():
    import json
    import shutil
    import subprocess

    from semantica_dataagent import k8s_sandbox

    wrapper = getattr(k8s_sandbox, "EXEC_WRAPPER", None)
    assert wrapper, "exec must receive credentials over stdin"
    node = shutil.which("node")
    assert node, "Node is required for the runner protocol test"
    secret = "example-secret-for-test"
    command = [
        node,
        "-e",
        wrapper,
        node,
        "-e",
        "process.stdin.on('data', x => console.log(process.env.TOKEN + ':' + x))",
    ]
    assert all(secret not in arg for arg in command)
    result = subprocess.run(
        command,
        input=json.dumps({"TOKEN": secret}) + "\nhello",
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "example-secret-for-test:hello"


def test_firewall_commands_allow_only_resolved_endpoints_and_dns(tmp_path):
    import os
    import subprocess

    log = tmp_path / "rules"
    helpers = {
        "node": """#!/bin/sh
case "$3:$4" in
  4:gateway.example) echo 203.0.113.10 ;;
  6:gateway.example) echo 2001:db8::10 ;;
  4:dataagent.semantica-dataagent.svc) echo 10.96.0.25 ;;
  6:dataagent.semantica-dataagent.svc) echo 2001:db8::25 ;;
esac
""",
        "awk": "#!/bin/sh\necho 10.96.0.10\n",
        "iptables": '#!/bin/sh\nprintf \'iptables %s\\n\' "$*" >> "$FIREWALL_LOG"\n',
        "ip6tables": '#!/bin/sh\nprintf \'ip6tables %s\\n\' "$*" >> "$FIREWALL_LOG"\n',
    }
    for name, content in helpers.items():
        helper = tmp_path / name
        helper.write_text(content)
        helper.chmod(0o755)
    env = {"PATH": str(tmp_path), "FIREWALL_LOG": str(log)}
    script = pod_spec()["spec"]["initContainers"][0]["args"][0]
    subprocess.run(["/bin/sh", "-ec", script], env=env, check=True, timeout=10)
    rules = log.read_text().splitlines()
    assert "iptables -P OUTPUT DROP" in rules
    assert "ip6tables -P OUTPUT DROP" in rules
    destinations = [line for line in rules if "--ctorigdst" in line]
    assert set(destinations) == {
        "iptables -A OUTPUT -p tcp -m conntrack --ctorigdst 203.0.113.10 --dport 443 -j ACCEPT",
        "iptables -A OUTPUT -p tcp -m conntrack --ctorigdst 10.96.0.25 --dport 8000 -j ACCEPT",
    }
    assert not any(
        "-A OUTPUT" in line
        and "ACCEPT" in line
        and "--ctorigdst" not in line
        and "ESTABLISHED,RELATED" not in line
        for line in rules
    )
    assert os.path.isfile(log)


def test_orphan_cleanup_selects_only_labeled_session_pods(tmp_path, monkeypatch):
    from semantica_dataagent.k8s_sandbox import cleanup_orphans

    log = tmp_path / "kubectl.log"
    binary = tmp_path / "kubectl"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CLEANUP_LOG"\n')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CLEANUP_LOG", str(log))
    cleanup_orphans()
    args = log.read_text().splitlines()
    assert args[:5] == ["-n", "semantica-dataagent-sessions", "delete", "pods", "-l"]
    assert args[5] == "app.kubernetes.io/component=agent-session"


def test_pod_rejects_workspace_traversal_and_credentialed_urls():
    import pytest

    from semantica_dataagent.k8s_sandbox import build_pod_spec

    base = {
        "name": "sda-agent-example",
        "workspace_subpath": "conversations/example",
        "model_url": "https://gateway.example/v1",
        "mcp_url": "http://dataagent.semantica-dataagent.svc:8000/mcp/",
    }
    for path in ("/conversations/example", "conversations/../other", "other/example"):
        with pytest.raises(ValueError):
            build_pod_spec(**(base | {"workspace_subpath": path}))
    with pytest.raises(ValueError):
        build_pod_spec(**(base | {"model_url": "https://user:secret@gateway.example/"}))


def test_session_upload_extracts_into_existing_root_owned_mounts(tmp_path):
    import subprocess

    import pytest

    from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox

    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode:
        pytest.skip("Docker daemon is required")
    (tmp_path / "work" / "config").mkdir(parents=True)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "work" / "config" / "models.json").write_text("{}")
    (tmp_path / "sessions" / "saved.jsonl").write_text("saved")
    runtime = object.__new__(KubernetesSessionSandbox)
    runtime.workspace = tmp_path

    def extract(direction, payload):
        assert direction == "upload"
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--user",
                "65534:65534",
                "--tmpfs",
                "/workspace:rw,mode=1777",
                "--tmpfs",
                "/sessions:rw,mode=1777",
                "--entrypoint",
                "tar",
                "semantica-dataagent-agent-runner:1.2.0",
                "--no-overwrite-dir",
                "--no-same-owner",
                "-xf",
                "-",
                "-C",
                "/",
            ],
            input=payload,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr.decode()

    runtime._transfer = extract
    runtime._upload()


def test_session_upload_preserves_mount_root_and_nested_files(tmp_path):
    import io
    import tarfile

    from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox

    (tmp_path / "work").mkdir()
    (tmp_path / "sessions").mkdir()
    (tmp_path / "work" / "file").write_text("one")
    (tmp_path / "sessions" / "file").write_text("two")
    runtime = object.__new__(KubernetesSessionSandbox)
    runtime.workspace = tmp_path
    contents = []

    def capture(direction, payload):
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            contents.extend(archive.getnames())

    runtime._transfer = capture
    runtime._upload()
    assert set(contents) == {"workspace/file", "sessions/file"}


def test_session_download_keeps_deletions_from_agent(tmp_path):
    import io
    import tarfile

    from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox

    for directory in ("work", "sessions"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "deleted").write_text("old")
    archive_data = io.BytesIO()
    with tarfile.open(fileobj=archive_data, mode="w") as archive:
        for directory in ("workspace", "sessions"):
            item = tarfile.TarInfo(directory)
            item.type = tarfile.DIRTYPE
            archive.addfile(item)
    runtime = object.__new__(KubernetesSessionSandbox)
    runtime.workspace = tmp_path
    runtime._transfer = lambda direction: archive_data.getvalue()
    runtime._download()
    assert not (tmp_path / "work" / "deleted").exists()
    assert not (tmp_path / "sessions" / "deleted").exists()
