"""Strict, provider-neutral extraction output models.

The models intentionally mirror ``schemas/extraction_batch.v1.json``.  A
provider may use any LLM or local heuristic, but it must return this small
auditable shape before anything is persisted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


EXTRACTION_SCHEMA_VERSION = "codememory.extraction_batch.v1"


class CandidateKind(StrEnum):
    ROUTE_OBSERVATION = "route_observation"
    DECISION = "decision"
    CONVENTION = "convention"
    FAILURE = "failure"
    VALIDATION = "validation"
    PREFERENCE = "preference"
    ANTI_BINDING = "anti_binding"


class BindingRole(StrEnum):
    PUBLIC_ENTRY = "public_entry"
    FEATURE_INTEGRATION = "feature_integration"
    SUPPORTING_SYMBOL = "supporting_symbol"
    MODIFIED_FILE = "modified_file"
    REJECTED_CANDIDATE = "rejected_candidate"


class LifecycleHint(StrEnum):
    OBSERVED = "observed"
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    STABLE = "stable"
    STALE = "stale"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class ExtractorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: BindingRole
    path: str | None = Field(default=None, max_length=4000)
    symbol: str | None = Field(default=None, max_length=1000)
    qualified_symbol: str | None = Field(default=None, max_length=2000)
    snapshot_id: str | None = Field(default=None, max_length=300)
    evidence: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("evidence")
    @classmethod
    def normalize_evidence(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_id: str = Field(min_length=1, max_length=300)
    kind: CandidateKind
    statement: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    bindings: list[Binding] = Field(default_factory=list, max_length=200)
    evidence_event_ids: list[str] = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    uncertainty: str = Field(min_length=1, max_length=2000)
    lifecycle_hint: LifecycleHint = LifecycleHint.CANDIDATE
    relation_hints: list[dict[str, Any]] = Field(default_factory=list, max_length=200)

    @field_validator("aliases", "evidence_event_ids")
    @classmethod
    def dedupe_strings(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result


class ExtractionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[EXTRACTION_SCHEMA_VERSION] = EXTRACTION_SCHEMA_VERSION
    extraction_run_id: str = Field(min_length=1, max_length=300)
    project_id: str = Field(min_length=1, max_length=300)
    task_id: str = Field(min_length=1, max_length=300)
    session_id: str | None = Field(default=None, max_length=300)
    source_event_ids: list[str] = Field(min_length=1, max_length=5000)
    extractor: ExtractorInfo
    candidates: list[Candidate] = Field(default_factory=list, max_length=500)

    @field_validator("source_event_ids")
    @classmethod
    def dedupe_source_ids(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result
