"""Kubernetes session Pod construction and lifecycle."""

import os
import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit

RUNNER_IMAGE = "172.16.242.124:31640/dataagent-agent-runner:1.2.1"
SESSION_LABEL = "app.kubernetes.io/component=agent-session"


def _endpoint(address: str) -> tuple[str, int]:
    url = urlsplit(address)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
    ):
        raise ValueError("invalid agent endpoint")
    return url.hostname, url.port or (443 if url.scheme == "https" else 80)


def _firewall(model_url: str, mcp_url: str) -> str:
    model_host, model_port = _endpoint(model_url)
    mcp_host, mcp_port = _endpoint(mcp_url)
    # Resolve before installing the policy; init is the only process running then.
    # conntrack's original destination preserves Service VIPs across kube-proxy DNAT.
    return f"""set -eu
model_host={shlex.quote(model_host)}
mcp_host={shlex.quote(mcp_host)}
resolve() {{
  node -e 'const dns=require("dns"), net=require("net"); const family=Number(process.argv[1]), host=process.argv[2]; const literal=net.isIP(host); if (literal) {{ if(literal===family) console.log(host); }} else dns.lookup(host, {{all:true}}, (err, ips) => {{ if(!err) console.log(ips.filter(ip => ip.family === family).map(ip => ip.address).join("\\n")); }});' "$1" "$2"
}}
resolve4() {{ resolve 4 "$1"; }}
resolve6() {{ resolve 6 "$1"; }}
model_ips=$(resolve4 "$model_host")
model_ips6=$(resolve6 "$model_host")
mcp_ips=$(resolve4 "$mcp_host")
mcp_ips6=$(resolve6 "$mcp_host")
[ -n "$model_ips$model_ips6" ] && [ -n "$mcp_ips$mcp_ips6" ]
dns_ip=$(awk '/^nameserver / {{print $2; exit}}' /etc/resolv.conf)
[ -n "$dns_ip" ]
iptables -P INPUT DROP
iptables -P OUTPUT DROP
ip6tables -P INPUT DROP
ip6tables -P OUTPUT DROP
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
for ip in $model_ips; do iptables -A OUTPUT -p tcp -m conntrack --ctorigdst "$ip" --dport {model_port} -j ACCEPT; done
for ip in $mcp_ips; do iptables -A OUTPUT -p tcp -m conntrack --ctorigdst "$ip" --dport {mcp_port} -j ACCEPT; done
for ip in $model_ips6; do ip6tables -A OUTPUT -p tcp -m conntrack --ctorigdst "$ip" --dport {model_port} -j ACCEPT; done
for ip in $mcp_ips6; do ip6tables -A OUTPUT -p tcp -m conntrack --ctorigdst "$ip" --dport {mcp_port} -j ACCEPT; done
case "$dns_ip" in
  *:*) dns_cmd=ip6tables ;;
  *) dns_cmd=iptables ;;
esac
for protocol in udp tcp; do "$dns_cmd" -A OUTPUT -p "$protocol" -m conntrack --ctorigdst "$dns_ip" --dport 53 -j ACCEPT; done
"""


def build_pod_spec(
    *, name: str, workspace_subpath: str, model_url: str, mcp_url: str
) -> dict:
    path = PurePosixPath(workspace_subpath)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != "conversations"
        or path.parts[1] in (".", "..")
    ):
        raise ValueError("invalid conversation workspace")
    spec = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {"fsGroup": 1000},
        "initContainers": [
            {
                "name": "firewall",
                "image": RUNNER_IMAGE,
                "command": ["sh", "-ec"],
                "args": [_firewall(model_url, mcp_url)],
                "securityContext": {
                    "runAsUser": 0,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]},
                },
            }
        ],
        "containers": [
            {
                "name": "agent",
                "image": RUNNER_IMAGE,
                "command": ["node", "-e", "setInterval(() => {}, 2147483647)"],
                "workingDir": "/workspace",
                "securityContext": {
                    "runAsUser": 1000,
                    "runAsGroup": 1000,
                    "runAsNonRoot": True,
                    "readOnlyRootFilesystem": True,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {"limits": {"cpu": "2", "memory": "2Gi"}},
                "volumeMounts": [
                    {
                        "name": "data",
                        "mountPath": "/workspace",
                        "subPath": f"{path}/work",
                    },
                    {
                        "name": "data",
                        "mountPath": "/sessions",
                        "subPath": f"{path}/sessions",
                    },
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "dataagent-data"}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    node = os.environ.get("DATAAGENT_NODE_NAME")
    if node:
        spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": "semantica-dataagent",
            "labels": {"app.kubernetes.io/component": "agent-session"},
        },
        "spec": spec,
    }


# The first stdin line carries the environment; neither credentials nor tokens
# appear in a Pod specification, kubectl arguments or a persisted workspace file.
EXEC_WRAPPER = r"""
const {spawn} = require('child_process');
let pending = Buffer.alloc(0);
function start(chunk) {
  pending = Buffer.concat([pending, chunk]);
  const end = pending.indexOf(10);
  if (end < 0) {
    if (pending.length > 1048576) process.exit(2);
    return;
  }
  process.stdin.removeListener('data', start);
  let env;
  try { env = JSON.parse(pending.subarray(0, end).toString('utf8')); }
  catch (_) { process.exit(2); }
  const child = spawn(process.argv[1], process.argv.slice(2), {
    env: {...process.env, ...env}, stdio: ['pipe', 'inherit', 'inherit']
  });
  child.on('error', () => process.exit(127));
  child.stdin.on('error', () => {});
  child.on('exit', (code) => process.exit(code === null ? 1 : code));
  const rest = pending.subarray(end + 1);
  if (rest.length) child.stdin.write(rest);
  process.stdin.pipe(child.stdin);
}
process.stdin.on('data', start);
"""


class KubernetesSessionSandbox:
    def __init__(self, workspace, model_url, mcp_url):
        import threading
        import uuid
        from pathlib import Path

        self.workspace = Path(workspace).resolve()
        root = Path(
            os.environ.get("DATAAGENT_AGENT_ROOT", "/data/conversations")
        ).resolve()
        if root != Path("/data/conversations") or self.workspace.parent != root:
            raise ValueError("workspace must be a direct child of /data/conversations")
        if not os.environ.get("DATAAGENT_NODE_NAME"):
            raise RuntimeError("DATAAGENT_NODE_NAME is required for the local PVC")
        self.name = "sda-agent-" + uuid.uuid4().hex
        self.model_url = model_url
        self.mcp_url = mcp_url
        self.lock = threading.RLock()
        self.closed = False
        self.spec = build_pod_spec(
            name=self.name,
            workspace_subpath=f"conversations/{self.workspace.name}",
            model_url=model_url,
            mcp_url=mcp_url,
        )

    def _run(self, *args, input=None, check=True):
        import subprocess

        return subprocess.run(
            ["kubectl", "-n", "semantica-dataagent", *args],
            input=input,
            text=True,
            capture_output=True,
            check=check,
            timeout=150,
        )

    def start(self):
        import json

        with self.lock:
            if self.closed:
                raise RuntimeError("session already ended")
            (self.workspace / "work").mkdir(parents=True, exist_ok=True)
            (self.workspace / "sessions").mkdir(parents=True, exist_ok=True)
            try:
                self._run("create", "-f", "-", input=json.dumps(self.spec))
                self._run(
                    "wait",
                    "--for=condition=Ready",
                    f"pod/{self.name}",
                    "--timeout=120s",
                )
            except BaseException:
                self.close()
                raise

    def wrap_command(self, command, env, keys):
        import json

        for key in ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"):
            if key in os.environ:
                env[key] = os.environ[key]
        values = {key: env[key] for key in keys if key in env}
        header = (json.dumps(values, ensure_ascii=True) + "\n").encode()
        return [
            "kubectl",
            "-n",
            "semantica-dataagent",
            "exec",
            "-i",
            self.name,
            "-c",
            "agent",
            "--",
            "node",
            "-e",
            EXEC_WRAPPER,
            *command,
        ], header

    def close(self):
        with self.lock:
            self.closed = True
            self._run(
                "delete",
                "pod",
                self.name,
                "--ignore-not-found",
                "--wait=true",
                "--timeout=120s",
            )


def cleanup_orphans():
    import subprocess

    subprocess.run(
        [
            "kubectl",
            "-n",
            "semantica-dataagent",
            "delete",
            "pods",
            "-l",
            SESSION_LABEL,
            "--wait=true",
            "--timeout=120s",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=150,
    )
