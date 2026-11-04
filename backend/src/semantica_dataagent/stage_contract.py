"""Shared contract for entity-writing stages."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StorageHandles:
    settings: dict[str, str]


@dataclass(frozen=True)
class StageResult:
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
