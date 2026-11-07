"""Check signed query downloads through the REST and MCP interfaces."""

import importlib.util
from pathlib import Path


def check_signed_downloads(ctx):
    spec = importlib.util.spec_from_file_location(
        "issue_82_checks", Path(__file__).with_name("issue_82.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_local_mcp_query(ctx)
