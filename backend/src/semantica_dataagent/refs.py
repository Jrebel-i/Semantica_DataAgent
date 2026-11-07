"""Bounded reference strings for the range indexes on entity nodes."""

import hashlib

MAX_REF_BYTES = 2048


def bounded_ref(value, counts=None):
    """Keep a deterministic, collision-resistant reference within index limits."""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_REF_BYTES:
        return value
    if counts is not None:
        counts["ref_truncated"] = counts.get("ref_truncated", 0) + 1
    suffix = "#" + hashlib.sha256(encoded).hexdigest()[:32]
    budget = MAX_REF_BYTES - len(suffix)
    prefix = encoded[:budget].decode("utf-8", "ignore")
    return prefix + suffix
