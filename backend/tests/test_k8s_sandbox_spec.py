"""Security contract for Kubernetes agent sessions and namespace permissions."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _docker_available():
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


def _require_docker():
    if not _docker_available():
        pytest.skip("Docker daemon is required")


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


def _node_executable():
    node = shutil.which("node")
    assert node, "Node is required for the PID1 command test"
    result = subprocess.run(
        [node, "-p", "process.execPath"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip() or node


def _cached_runner_image():
    _require_docker()
    from semantica_dataagent.k8s_sandbox import RUNNER_IMAGE

    result = subprocess.run(
        ["docker", "image", "inspect", RUNNER_IMAGE],
        capture_output=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        pytest.skip("the cached runner image is required for the container PID1 test")
    return RUNNER_IMAGE


SIGTERM_HANDLER = "process.on('SIGTERM', () => process.exit(0)); "


def test_session_pid1_command_exits_zero_on_sigterm():
    """The built command must exit 0, not take Node's default SIGTERM death.

    A host child is not PID1; the container test below and the real-Pod script
    cover PID1 semantics.  Without the explicit handler the built command exits
    from the default SIGTERM action with returncode -15, so this is a real
    behavioral check of the command extracted from the Pod spec.
    """
    import os
    import select
    import signal
    import time

    command = pod_spec()["spec"]["containers"][0]["command"]
    assert command[:2] == ["node", "-e"]
    script = command[2] + '; console.log("READY")'

    process = subprocess.Popen(
        [_node_executable(), command[1], script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        readable, _, _ = select.select([process.stdout], [], [], 5)
        assert readable, "command did not finish registering handlers"
        assert process.stdout.readline().strip() == "READY"
        assert process.poll() is None, "PID1 command must stay resident before SIGTERM"
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        assert time.monotonic() - started < 5
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert command[2].startswith(SIGTERM_HANDLER), (
        "PID1 command must install an explicit SIGTERM handler"
    )


def test_session_pid1_sigterm_behavior_as_container_init():
    import time
    import uuid

    term_bound = 3.0
    image = _cached_runner_image()
    command = pod_spec()["spec"]["containers"][0]["command"]
    legacy_script = command[2].removeprefix(SIGTERM_HANDLER)
    assert legacy_script.startswith("setInterval(")

    def start(name, script):
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--pull",
                "never",
                "--network",
                "none",
                "--entrypoint",
                "node",
                image,
                "-e",
                script,
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )

    def state(name):
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        running, exit_code = result.stdout.split()
        return running == "true", int(exit_code)

    def wait_running(name):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = state(name)
            if current is not None and current[0]:
                return current
            time.sleep(0.1)
        pytest.fail("container did not stay running")

    def wait_ready(name):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            logs = subprocess.run(
                ["docker", "logs", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if "READY" in logs.stdout:
                return
            time.sleep(0.1)
        pytest.fail("command did not finish registering handlers")

    def wait_stopped(name, deadline):
        while time.monotonic() < deadline:
            current = state(name)
            if current is not None and not current[0]:
                return current[1]
            time.sleep(0.1)
        pytest.fail(f"container did not stop before the deadline: {state(name)}")

    def kill(name, *args):
        subprocess.run(
            ["docker", "kill", *args, name],
            check=True,
            capture_output=True,
            timeout=10,
        )

    def remove(name):
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, check=False, timeout=15
        )

    marker = '; console.log("READY")'
    legacy = f"sda-issue262-{uuid.uuid4().hex}"
    try:
        start(legacy, legacy_script + marker)
        wait_running(legacy)
        wait_ready(legacy)
        kill(legacy, "--signal=TERM")
        deadline = time.monotonic() + term_bound
        while time.monotonic() < deadline:
            current = state(legacy)
            assert current is not None and current[0], (
                "legacy PID1 command must stay running for the whole "
                f"{term_bound}s after SIGTERM; state={current}"
            )
            time.sleep(0.1)
        kill(legacy)
        assert wait_stopped(legacy, time.monotonic() + term_bound) == 137
    finally:
        remove(legacy)

    fixed = f"sda-issue262-{uuid.uuid4().hex}"
    try:
        start(fixed, command[2] + marker)
        wait_running(fixed)
        wait_ready(fixed)
        kill(fixed, "--signal=TERM")
        assert wait_stopped(fixed, time.monotonic() + term_bound) == 0
    finally:
        remove(fixed)


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
    from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox

    _require_docker()
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


def test_session_upload_skips_when_docker_command_is_missing(monkeypatch, tmp_path):
    calls = []
    original_run = subprocess.run

    def record(*args, **kwargs):
        calls.append(args)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(shutil, "which", lambda command: None)
    monkeypatch.setattr(subprocess, "run", record)
    with pytest.raises(pytest.skip.Exception, match="Docker daemon is required"):
        test_session_upload_extracts_into_existing_root_owned_mounts(tmp_path)
    assert calls == []


def test_session_upload_skips_when_docker_daemon_is_unavailable(monkeypatch, tmp_path):
    calls = []

    def unavailable(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stderr=b"daemon unavailable")

    monkeypatch.setattr(shutil, "which", lambda command: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(pytest.skip.Exception, match="Docker daemon is required"):
        test_session_upload_extracts_into_existing_root_owned_mounts(tmp_path)
    assert calls == [["docker", "info"]]


def test_session_upload_uses_docker_run_when_docker_is_available(monkeypatch, tmp_path):
    calls = []

    def available(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stderr=b"")

    monkeypatch.setattr(shutil, "which", lambda command: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", available)
    test_session_upload_extracts_into_existing_root_owned_mounts(tmp_path)
    assert [argv for argv, _ in calls] == [
        ["docker", "info"],
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
    ]
    payload = calls[1][1]["input"]
    assert b"workspace/config/models.json" in payload
    assert b"sessions/saved.jsonl" in payload


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


def test_session_download_handles_files_replaced_by_directories_and_reverse(tmp_path):
    import io
    import tarfile

    from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox

    (tmp_path / "work").mkdir()
    (tmp_path / "sessions").mkdir()
    (tmp_path / "work" / "directory_now").write_text("old file")
    (tmp_path / "work" / "file_now").mkdir()
    (tmp_path / "work" / "file_now" / "old").write_text("old")
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name in ("workspace", "sessions", "workspace/directory_now"):
            item = tarfile.TarInfo(name)
            item.type = tarfile.DIRTYPE
            archive.addfile(item)
        for name, value in (
            ("workspace/directory_now/new", b"nested"),
            ("workspace/file_now", b"file"),
        ):
            item = tarfile.TarInfo(name)
            item.size = len(value)
            archive.addfile(item, io.BytesIO(value))
    runtime = object.__new__(KubernetesSessionSandbox)
    runtime.workspace = tmp_path
    runtime._transfer = lambda direction: data.getvalue()
    runtime._download()
    assert (tmp_path / "work" / "directory_now" / "new").read_text() == "nested"
    assert (tmp_path / "work" / "file_now").read_text() == "file"


def test_session_download_rejects_incomplete_archive_before_changing_workspace(
    tmp_path,
):
    import io
    import tarfile

    import pytest

    from semantica_dataagent.k8s_sandbox import KubernetesSessionSandbox

    (tmp_path / "work").mkdir()
    (tmp_path / "sessions").mkdir()
    (tmp_path / "work" / "existing").write_text("original")
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        entry = tarfile.TarInfo("workspace")
        entry.type = tarfile.DIRTYPE
        archive.addfile(entry)
        entry = tarfile.TarInfo("workspace/existing")
        entry.size = len(b"replacement")
        archive.addfile(entry, io.BytesIO(b"replacement"))
    runtime = object.__new__(KubernetesSessionSandbox)
    runtime.workspace = tmp_path
    runtime._transfer = lambda direction: payload.getvalue()
    with pytest.raises((ValueError, OSError)):
        runtime._download()
    assert (tmp_path / "work" / "existing").read_text() == "original"
