"""会话运行时的隔离契约。"""

from pathlib import Path
from unittest.mock import patch

import yaml


def test_session_pod_uses_resolved_hosts_and_independent_namespace():
    from semantica_dataagent.k8s_sandbox import build_pod_spec

    def resolve(host, *args, **kwargs):
        addresses = {"gateway.example": "203.0.113.10", "mcp.example": "192.0.2.20"}
        return [(2, 1, 6, "", (addresses[host], 0))]

    with patch("socket.getaddrinfo", side_effect=resolve):
        pod = build_pod_spec(
            name="example",
            workspace_subpath="conversations/example",
            model_url="https://gateway.example/v1",
            mcp_url="http://mcp.example:8000/mcp",
        )
    assert pod["metadata"]["namespace"] == "semantica-dataagent-sessions"
    aliases = {entry["ip"]: entry["hostnames"] for entry in pod["spec"]["hostAliases"]}
    assert aliases == {
        "203.0.113.10": ["gateway.example"],
        "192.0.2.20": ["mcp.example"],
    }
    firewall = pod["spec"]["initContainers"][0]["args"][0]
    assert "203.0.113.10" in firewall and "192.0.2.20" in firewall
    assert "--dport 53" not in firewall
    assert "resolve()" not in firewall


def test_session_rbac_is_only_in_session_namespace():
    directory = Path(__file__).resolve().parents[2] / "deploy/k8s/app"
    docs = [
        doc
        for path in directory.glob("*.yaml")
        for doc in yaml.safe_load_all(path.read_text())
        if isinstance(doc, dict)
    ]
    roles = [doc for doc in docs if doc.get("kind") == "Role"]
    assert roles
    assert all(
        role["metadata"]["namespace"] == "semantica-dataagent-sessions"
        for role in roles
    )
    bindings = [doc for doc in docs if doc.get("kind") == "RoleBinding"]
    assert bindings and all(
        binding["metadata"]["namespace"] == "semantica-dataagent-sessions"
        for binding in bindings
    )


def test_runtime_selection_rejects_unknown_modes(monkeypatch):
    import pytest

    from semantica_dataagent.session_runtime import LocalSessionRuntime, runtime_class

    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "local")
    assert runtime_class() is LocalSessionRuntime
    monkeypatch.setenv("DATAAGENT_AGENT_MODE", "misspelled")
    with pytest.raises(ValueError, match="unknown session runtime"):
        runtime_class()


def test_session_firewall_covers_ipv6_and_custom_namespace(monkeypatch):
    from semantica_dataagent.k8s_sandbox import build_pod_spec

    monkeypatch.setenv("DATAAGENT_SESSION_NAMESPACE", "custom-sessions")
    addresses = {
        "gateway.example": ["203.0.113.10", "2001:db8::10"],
        "mcp.example": ["192.0.2.20", "2001:db8::20"],
    }

    def resolve(host, *args, **kwargs):
        return [(10 if ":" in ip else 2, 1, 6, "", (ip, 0)) for ip in addresses[host]]

    with patch("socket.getaddrinfo", side_effect=resolve):
        pod = build_pod_spec(
            name="example",
            workspace_subpath="conversations/example",
            model_url="https://gateway.example/v1",
            mcp_url="http://mcp.example:8000/mcp",
        )
    assert pod["metadata"]["namespace"] == "custom-sessions"
    script = pod["spec"]["initContainers"][0]["args"][0]
    assert "ip6tables -P OUTPUT DROP" in script
    assert "--ctorigdst 2001:db8::10 --dport 443" in script
    assert "--ctorigdst 2001:db8::20 --dport 8000" in script
    assert "--dport 53" not in script
