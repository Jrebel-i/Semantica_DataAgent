"""Time fields exposed by the REST API.

API timestamps are ISO 8601 with an explicit UTC offset.  SQLite returns
stored datetimes without ``tzinfo``; those values are UTC by convention, so
serialization attaches UTC instead of exposing a naive local-looking value.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BeforeValidator


def as_utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return value


def as_utc_or_now(value: datetime | str | None) -> datetime:
    """Normalize a datetime/ISO string to UTC, defaulting ``None`` to current UTC time."""
    if value is None:
        return datetime.now(UTC)
    parsed = as_utc(value)
    return parsed if isinstance(parsed, datetime) else datetime.now(UTC)


UtcDatetime = Annotated[datetime, BeforeValidator(as_utc)]
