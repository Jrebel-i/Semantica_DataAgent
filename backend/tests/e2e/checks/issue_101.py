"""Exercise issue 101 through the shared fake DTB REST check."""

import runpy
from pathlib import Path


def check_issue_101(ctx):
    check = runpy.run_path(str(Path(__file__).with_name("issue_76.py")))
    return check["check_dtb_pull"](ctx)
