"""Security contract for Kubernetes agent sessions and namespace permissions."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def pod_spec():
    module = importlib.util.find_spec("semantica_dataagent.k8s_sandbox")
    assert module is not None, "Kubernetes session sandbox must be implemented"
    from semantica_dataagent.k8s_sandbox import build_pod_spec

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
    assert mounts and all(
        m["subPath"].startswith("conversations/example/") for m in mounts
    )


def test_session_firewall_denies_ipv4_and_ipv6_by_default():
    pod = pod_spec()
    init = pod["spec"]["initContainers"][0]
    assert "NET_ADMIN" in init["securityContext"]["capabilities"]["add"]
    script = "\n".join(init.get("command", []) + init.get("args", []))
    for command in ("iptables", "ip6tables"):
        assert command in script
        assert f"{command} -P OUTPUT DROP" in script
    assert "gateway.example" in script
    assert "dataagent.semantica-dataagent.svc" in script


def test_application_rbac_is_namespace_scoped_and_minimal():
    docs = manifests()
    roles = [d for d in docs if d.get("kind") == "Role"]
    assert roles, "application Role is required"
    assert not any(d.get("kind") in ("ClusterRole", "ClusterRoleBinding") for d in docs)
    for role in roles:
        assert role["metadata"]["namespace"] == "semantica-dataagent"
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
