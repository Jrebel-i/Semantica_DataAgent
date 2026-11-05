"""Check the deployed frontend and the actual session Pod firewall."""

import json
import shlex
import subprocess
import uuid

import httpx

from semantica_dataagent.k8s_sandbox import build_pod_spec

NAMESPACE = "semantica-dataagent"
HOST = "baize@172.16.242.124"
BASE = "http://172.16.242.124:31630"


def _kubectl(*args, input_data=None, check=True, timeout=180):
    command = shlex.join(["kubectl", "-n", NAMESPACE, *args])
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST, command],
        input=input_data,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"kubectl {args[0]} failed: {result.stderr[-1000:]}")
    return result


def check_frontend_and_mcp(ctx):
    response = httpx.get(BASE + "/", timeout=15, trust_env=False)
    ctx.require(
        response.status_code == 200, f"cluster frontend HTTP {response.status_code}"
    )
    ctx.require(
        'id="root"' in response.text, "frontend index is missing its application root"
    )
    denied = httpx.get(BASE + "/mcp/", timeout=15, trust_env=False)
    ctx.require(
        denied.status_code == 401, f"unauthenticated MCP HTTP {denied.status_code}"
    )
    hits = ctx.mcp_call(
        "search_knowledge", {"query": "销售额", "scope": "docs", "top_k": 5}
    )
    ctx.require(isinstance(hits, list) and hits, "MCP search returned no knowledge")
    return {"frontend_http": response.status_code, "mcp_search_hits": len(hits)}


def check_session_egress(ctx):
    suffix = uuid.uuid4().hex[:12]
    name = "sda-agent-e2e26-" + suffix
    session = "issue26_" + suffix
    directory = "/data/conversations/" + session
    pods = json.loads(
        _kubectl("get", "pods", "-l", "app=dataagent", "-o", "json").stdout
    )["items"]
    ready = [p for p in pods if p["status"].get("phase") == "Running"]
    ctx.require(len(ready) == 1, "expected exactly one running application Pod")
    node = ready[0]["spec"]["nodeName"]
    created_workspace = False
    created_pod = False
    try:
        _kubectl(
            "exec",
            "deploy/dataagent",
            "--",
            "mkdir",
            "-p",
            directory + "/work",
            directory + "/sessions",
        )
        created_workspace = True
        pod = build_pod_spec(
            name=name,
            workspace_subpath="conversations/" + session,
            model_url="https://llm-cn.pkfare-inc.com",
            mcp_url="http://dataagent.semantica-dataagent.svc:8000/mcp/",
        )
        pod["spec"]["nodeSelector"] = {"kubernetes.io/hostname": node}
        _kubectl("create", "-f", "-", input_data=json.dumps(pod))
        created_pod = True
        _kubectl("wait", "--for=condition=Ready", "pod/" + name, "--timeout=120s")
        script = r"""const http=require('http'), https=require('https'), net=require('net');
function req(url){return new Promise(resolve=>{const r=(url.startsWith('https:')?https:http).get(url,{timeout:4000},s=>{s.resume();resolve('HTTP '+s.statusCode)});r.on('timeout',()=>r.destroy());r.on('error',e=>resolve('ERROR '+e.code));});}
function conn(host,port){return new Promise(resolve=>{const s=net.connect({host,port,timeout:4000});s.on('connect',()=>{s.destroy();resolve('CONNECTED')});s.on('timeout',()=>s.destroy());s.on('error',e=>resolve('ERROR '+e.code));s.on('close',()=>resolve('CLOSED'));});}
(async()=>{const result={mcp:await req('http://dataagent.semantica-dataagent.svc:8000/mcp/'),model:await req('https://llm-cn.pkfare-inc.com/'),external:await req('https://example.com/'),neo4j:await conn('neo4j.semantica-dataagent.svc',7687),api:await conn('kubernetes.default.svc',443),node:await conn('172.16.242.124',31640)};console.log(JSON.stringify(result));})();"""
        result = _kubectl(
            "exec", "-i", name, "-c", "agent", "--", "node", input_data=script
        )
        traffic = json.loads(result.stdout.strip())
        ctx.require(traffic["mcp"].startswith("HTTP "), f"MCP unreachable: {traffic}")
        ctx.require(
            traffic["model"].startswith("HTTP "),
            f"model gateway unreachable: {traffic}",
        )
        ctx.require(
            not traffic["external"].startswith("HTTP "),
            f"external egress allowed: {traffic}",
        )
        for target in ("neo4j", "api", "node"):
            ctx.require(
                traffic[target] != "CONNECTED", f"{target} egress allowed: {traffic}"
            )
        flush = _kubectl(
            "exec", name, "-c", "agent", "--", "iptables", "-F", check=False
        )
        ctx.require(
            flush.returncode != 0 and "ermission denied" in flush.stderr,
            "agent could alter firewall",
        )
        return traffic
    finally:
        try:
            if created_pod:
                _kubectl(
                    "delete",
                    "pod",
                    name,
                    "--ignore-not-found",
                    "--wait=true",
                    "--timeout=120s",
                )
        finally:
            if created_workspace:
                _kubectl(
                    "exec",
                    "deploy/dataagent",
                    "--",
                    "python",
                    "-c",
                    "import shutil,sys; shutil.rmtree(sys.argv[1])",
                    directory,
                )
