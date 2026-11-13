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
POD_PHASES = frozenset({"Pending", "Running", "Succeeded", "Failed", "Unknown"})
POD_REASONS = frozenset(
    {
        "ContainerCreating",
        "ContainerStatusUnknown",
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
        "DeadlineExceeded",
        "ErrImagePull",
        "Error",
        "Evicted",
        "ImagePullBackOff",
        "InvalidImageName",
        "NodeAffinity",
        "NodeLost",
        "OOMKilled",
        "PodInitializing",
        "RunContainerError",
        "SchedulingGated",
        "Shutdown",
        "TerminationByKubelet",
        "Unschedulable",
        "UnexpectedAdmissionError",
    }
)
KNOWN_STAGES = DTB_STAGES | QUERY_STAGES | SESSION_STAGES
KNOWN_CATEGORIES = DTB_CATEGORIES | SESSION_CATEGORIES


def _safe_value(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def safe_dtb_stage(value):
    return _safe_value(value, DTB_STAGES)


def safe_dtb_category(value):
    return _safe_value(value, DTB_CATEGORIES)


def safe_query_stage(value):
    return _safe_value(value, QUERY_STAGES)


def safe_session_stage(value):
    return _safe_value(value, SESSION_STAGES)


def safe_session_category(value):
    return _safe_value(value, SESSION_CATEGORIES)


def _diagnostic_pair(exc):
    stage = getattr(exc, "failure_stage", None)
    category = getattr(exc, "failure_category", None)
    if (
        isinstance(stage, str)
        and stage in KNOWN_STAGES
        and isinstance(category, str)
        and category in KNOWN_CATEGORIES
    ):
        return stage, category
    return "unknown", "unknown"


def failure_label(exc):
    """Return a fixed label; untrusted diagnostic attributes degrade to unknown."""
    stage, category = _diagnostic_pair(exc)
    return f"{stage}/{category}"


def safe_pod_status(value):
    """Keep only allow-listed Pod status strings from a raw status mapping."""
    if not isinstance(value, dict):
        return None
    details = {}
    phase = value.get("phase")
    if isinstance(phase, str):
        details["phase"] = _safe_value(phase, POD_PHASES)
    reason = value.get("reason")
    if isinstance(reason, str):
        details["reason"] = _safe_value(reason, POD_REASONS)
    container_reason = value.get("container_reason")
    if isinstance(container_reason, str):
        details["container_reason"] = _safe_value(container_reason, POD_REASONS)
    return details or None


def failure_details(exc):
    """Return the safe fields that may be persisted or streamed."""
    stage, category = _diagnostic_pair(exc)
    details = {"stage": stage, "category": category}
    pod_status = safe_pod_status(getattr(exc, "pod_status", None))
    if pod_status is not None:
        details["pod_status"] = pod_status
    return details
