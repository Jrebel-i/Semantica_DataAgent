"""Initialize a new Langfuse namespace; credentials only travel over stdin."""

import json
import secrets
import subprocess
from pathlib import Path

import yaml

HOST = "baize@172.16.242.124"
NS = "semantica-dataagent-langfuse"


def remote(command, data=None, check=True):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOST, command],
        input=data,
        text=True,
        check=check,
    )


def secret(name, data):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": NS},
        "stringData": data,
    }


if __name__ == "__main__":
    if (
        remote(f"kubectl get namespace {NS} >/dev/null 2>&1", check=False).returncode
        == 0
    ):
        raise SystemExit("Namespace already exists; refusing to replace credentials")
    remote(f"kubectl create namespace {NS}")
    password = secrets.token_hex(24)
    test = {
        "public-key": "pk-lf-" + secrets.token_hex(16),
        "secret-key": "sk-lf-" + secrets.token_hex(24),
    }
    dev = {
        "public-key": "pk-lf-" + secrets.token_hex(16),
        "secret-key": "sk-lf-" + secrets.token_hex(24),
    }
    config = {
        "password": password,
        "database-url": f"postgresql://postgres:{password}@localhost:5432/postgres",
        "salt": secrets.token_hex(32),
        "encryption-key": secrets.token_hex(32),
        "nextauth-secret": secrets.token_hex(32),
        "admin-password": secrets.token_hex(24),
    }
    payload = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            secret("langfuse-config", config),
            secret("langfuse-test-keys", test),
            secret("langfuse-dev-keys", dev),
        ],
    }
    remote("kubectl apply -f -", json.dumps(payload))
    objects = list(
        yaml.safe_load_all(Path(__file__).with_name("langfuse.yaml").read_text())
    )
    deployment = next(item for item in objects if item["kind"] == "Deployment")
    web = next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "web"
    )
    for item in web["env"]:
        if item["name"] in ("LANGFUSE_INIT_PROJECT_ID", "LANGFUSE_INIT_PROJECT_NAME"):
            item["value"] = "dataagent-test"
        if item["name"] in (
            "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
            "LANGFUSE_INIT_PROJECT_SECRET_KEY",
        ):
            item["valueFrom"]["secretKeyRef"]["name"] = "langfuse-test-keys"
    remote("kubectl apply -f -", yaml.safe_dump_all(objects))
    remote(f"kubectl -n {NS} rollout status deployment/langfuse --timeout=600s")
    remote(
        f"kubectl -n {NS} wait --for=jsonpath='{{.status.phase}}'=Running pod -l app=langfuse --timeout=60s"
    )
    # The web process can be running before its migrations and initialization finish.
    remote(
        f"kubectl -n {NS} exec deployment/langfuse -c web -- sh -c 'until wget -qO- http://localhost:3000/api/public/health >/dev/null; do sleep 2; done'"
    )
    remote("kubectl apply -f -", Path(__file__).with_name("langfuse.yaml").read_text())
    remote(f"kubectl -n {NS} rollout status deployment/langfuse --timeout=600s")
