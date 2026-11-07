"""Issue #121 end-to-end checks: clarification and query-plan evaluation."""

import sys
from pathlib import Path

_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.append(_CHECKS_DIR)

from issue_85 import check_execution_evaluation


def check_eval_clarification_and_query_plan(ctx):
    """Exercise the #121 evaluator paths through the #85 fake agent and DTB."""
    return check_execution_evaluation(ctx)
