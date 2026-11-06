"""DTB authentication failure detection shared by the puller and executor."""

AUTHENTICATION_STATUS_CODES = frozenset({401, 415, 503})
AUTHENTICATION_BODY_STATUSES = frozenset({"401", "415", "503"})


def authentication_failed(status_code, payload):
    """Return whether a DTB response reports an expired or missing login.

    The real DTB gateway answers authentication failures with HTTP 200 and a
    JSON body carrying ``status`` as ``"401"``, ``"415"``, or ``"503"``. Some
    deployments instead use the corresponding HTTP status code.
    """
    if status_code in AUTHENTICATION_STATUS_CODES:
        return True
    return (
        isinstance(payload, dict)
        and str(payload.get("status")) in AUTHENTICATION_BODY_STATUSES
    )
