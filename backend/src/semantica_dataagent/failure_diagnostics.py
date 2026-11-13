"""Safe, fixed failure diagnostics for executor and session boundaries.

Raw platform messages may contain SQL, literals, result rows, command input or
credentials.  Only the fixed values in this module may cross a logging, SSE or
persistence boundary.
"""

DTB_STAGES = frozenset({"submit", "poll", "download", "unknown"})
QUERY_STAGES = frozenset(
    {"validate_sql", "table_catalog", "submit", "wait", "result", "unknown"}
)
DTB_CATEGORIES = frozenset(
    {
        "table_or_catalog",
        "sql",
        "authentication",
        "data_source",
        "timeout",
        "result",
        "unknown",
    }
)

SESSION_STAGES = frozenset({"create", "wait", "upload", "unknown"})
SESSION_CATEGORIES = frozenset({"nonzero_exit", "timeout", "startup_error", "unknown"})
POD_STATUS_FIELDS = ("phase", "reason", "container_reason")
KNOWN_STAGES = DTB_STAGES | QUERY_STAGES | SESSION_STAGES
KNOWN_CATEGORIES = DTB_CATEGORIES | SESSION_CATEGORIES


def safe_dtb_stage(value):
    return value if value in DTB_STAGES else "unknown"


def safe_dtb_category(value):
    return value if value in DTB_CATEGORIES else "unknown"


def safe_query_stage(value):
    return value if value in QUERY_STAGES else "unknown"


def safe_session_stage(value):
    return value if value in SESSION_STAGES else "unknown"


def safe_session_category(value):
    return value if value in SESSION_CATEGORIES else "unknown"


def failure_label(exc):
    """Return a fixed label for a diagnostic exception, or its class name."""
    stage = getattr(exc, "failure_stage", None)
    category = getattr(exc, "failure_category", None)
    if (
        isinstance(stage, str)
        and stage in KNOWN_STAGES
        and isinstance(category, str)
        and category in KNOWN_CATEGORIES
    ):
        return f"{stage}/{category}"
    return type(exc).__name__


def safe_pod_status(value):
    """Keep only allow-listed Pod status strings from a raw status mapping."""
    if not isinstance(value, dict):
        return None
    details = {
        key: value[key][:64]
        for key in POD_STATUS_FIELDS
        if isinstance(value.get(key), str) and value[key]
    }
    return details or None


def failure_details(exc):
    """Return the safe fields that may be persisted or streamed."""
    stage = getattr(exc, "failure_stage", None)
    category = getattr(exc, "failure_category", None)
    if (
        not isinstance(stage, str)
        or stage not in KNOWN_STAGES
        or not isinstance(category, str)
        or category not in KNOWN_CATEGORIES
    ):
        return None
    details = {"stage": stage, "category": category}
    pod_status = safe_pod_status(getattr(exc, "pod_status", None))
    if pod_status is not None:
        details["pod_status"] = pod_status
    return details
