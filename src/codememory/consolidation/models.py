"""Small public models for the Phase 3 card lifecycle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CardStatus(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    STABLE = "stable"
    UNCERTAIN = "uncertain"
    STALE = "stale"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class ConsolidationAction(StrEnum):
    CREATE = "create"
    MERGE = "merge"
    NEW_VERSION = "new_version"
    PROMOTE = "promote"
    SUPERSEDE = "supersede"
    CONTRADICT = "contradict"
    REJECT = "reject"
    UNCERTAIN = "uncertain"
    SKIP = "skip"


@dataclass(frozen=True)
class ConsolidationItem:
    candidate_id: str
    action: str
    card_id: str | None
    card_version_id: str | None
    status: str | None
    score: float | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConsolidationResult:
    candidate_count: int
    processed: int
    created: int
    merged: int
    new_versions: int
    uncertain: int
    skipped: int
    rejected: int
    items: tuple[ConsolidationItem, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["items"] = [item.as_dict() for item in self.items]
        return result
