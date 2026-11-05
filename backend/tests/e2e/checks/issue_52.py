"""Verify independent imports and authenticated access through the running app."""

import importlib
import pkgutil
import subprocess
import sys


def check_independent_imports_and_auth(ctx):
    package = importlib.import_module("semantica_dataagent")
    modules = sorted(
        item.name
        for item in pkgutil.walk_packages(package.__path__, package.__name__ + ".")
    )
    for module in modules:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib, sys; importlib.import_module(sys.argv[1])",
                module,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        ctx.require(result.returncode == 0, f"{module} import failed: {result.stderr}")
    response = ctx.rest.get("/api/v1/me")
    ctx.require(
        response.status_code == 200, f"authenticated REST failed: {response.text}"
    )
    ctx.mcp_call("get_capabilities", {})
    return {"modules": len(modules)}
