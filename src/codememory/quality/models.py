"""Stable, JSON-friendly models emitted by the continuous quality gate.

The gate deliberately uses a small vocabulary.  ``accepted`` means an
observation is safe to enter the governed candidate/card pipeline; ``review``
means it is useful but needs a human or a later source verifier; and
``quarantine`` means it must remain auditable without being allowed to create
or mutate a memory card.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


EVENT_QUALITY_SCHEMA_VERSION = "codememory.event_quality.v1"
CANDIDATE_QUALITY_SCHEMA_VERSION = "codememory.candidate_quality.v1"
# Bump whenever deterministic rules or their dimensions change.  Persisting
# the version makes an old projection visibly stale and forces the next
# extraction/replay to re-evaluate it instead of silently reusing v1 rows.
QUALITY_CLASSIFIER_VERSION = "coding-quality-gate-v2"


class QualityDecision(StrEnum):
    ACCEPTED = "accepted"
    REVIEW = "review"
    QUARANTINE = "quarantine"


class EventRole(StrEnum):
    INTENT = "intent"
    CODE_EVIDENCE = "code_evidence"
    OUTCOME = "outcome"
    FEEDBACK = "feedback"
    LIFECYCLE = "lifecycle"
    NOISE = "noise"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EventQualityEvaluation:
    event_id: str
    project_id: str
    task_id: str
    role: str
    decision: str
    signal_score: float
    dimensions: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    classifier_version: str = QUALITY_CLASSIFIER_VERSION
    evaluated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True)
class CandidateQualityReview:
    candidate_id: str
    project_id: str
    task_id: str
    decision: str
    quality_score: float
    dimensions: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    classifier_version: str = QUALITY_CLASSIFIER_VERSION
    reviewed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True)
class QualityRunResult:
    run_id: str
    scope: str
    mode: str
    status: str
    event_count: int
    candidate_count: int
    logical_project_count: int
    event_decisions: dict[str, int]
    candidate_decisions: dict[str, int]
    alias_suggestions: int
    input_hash: str
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
