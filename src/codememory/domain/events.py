"""Versioned coding-event envelope.

The envelope is deliberately independent of any particular agent. Adapters
translate their native callbacks into this contract; the storage layer never
needs to know whether an event came from Codex, Pi, an IDE, or a replay file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EVENT_SCHEMA_VERSION = "codememory.event.v1"


class EventType(StrEnum):
    SESSION_STARTED = "session_started"
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_READ = "file_read"
    FILE_EDIT = "file_edit"
    COMMAND_RUN = "command_run"
    VALIDATION_RUN = "validation_run"
    VCS_CHANGE = "vcs_change"
    USER_FEEDBACK = "user_feedback"
    SESSION_ENDED = "session_ended"


class EventSource(StrEnum):
    AGENT = "agent"
    API = "api"
    HOOK = "hook"
    REPLAY = "replay"
    UNKNOWN = "unknown"


class EventCompleteness(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    SUMMARY = "summary"


class Producer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_id: str = Field(min_length=1, max_length=200)
    adapter: str = Field(min_length=1, max_length=100)
    adapter_version: str = Field(min_length=1, max_length=100)


class EventContext(BaseModel):
    """Best-effort code context known at event capture time.

    Adapters may add provider-specific fields. Unknown values are retained in
    this nested object so the stable envelope can evolve without losing data.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    repo_id: str | None = Field(default=None, max_length=300)
    root_path: str | None = Field(default=None, max_length=2000)
    branch: str | None = Field(default=None, max_length=500)
    commit_id: str | None = Field(default=None, max_length=200)
    p4_changelist: str | None = Field(default=None, max_length=100)
    cwd: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def values_must_be_json_serializable(self) -> "EventContext":
        import json

        json.dumps(self.model_dump(mode="python"), ensure_ascii=False, allow_nan=False)
        return self


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    kind: str = Field(min_length=1, max_length=100)
    uri: str | None = Field(default=None, max_length=4000)
    path: str | None = Field(default=None, max_length=4000)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    mime_type: str | None = Field(default=None, max_length=200)
    size_bytes: int | None = Field(default=None, ge=0)
    content: str | None = Field(default=None, max_length=20_000_000)
    content_encoding: Literal["utf-8", "base64"] = "utf-8"
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_must_be_json_serializable(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value


class RedactionInfo(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    applied: bool = False
    ruleset_version: str = "builtin-v1"
    fields: list[str] = Field(default_factory=list)


class EventEnvelope(BaseModel):
    """Canonical event accepted by both HTTP ingest and JSONL replay."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[EVENT_SCHEMA_VERSION] = EVENT_SCHEMA_VERSION
    event_id: str = Field(min_length=1, max_length=300)
    external_event_id: str | None = Field(default=None, max_length=500)
    event_type: EventType
    occurred_at: datetime
    producer: Producer
    project_id: str = Field(min_length=1, max_length=300)
    task_id: str = Field(min_length=1, max_length=300)
    session_id: str | None = Field(default=None, max_length=300)
    seq: int = Field(ge=0)
    parent_event_id: str | None = Field(default=None, max_length=300)
    context: EventContext = Field(default_factory=EventContext)
    payload: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    redaction: RedactionInfo = Field(default_factory=RedactionInfo)
    source: EventSource = EventSource.UNKNOWN
    completeness: EventCompleteness = EventCompleteness.FULL

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("payload")
    @classmethod
    def payload_must_be_json_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Pydantic has already checked the shape. Reject non-JSON values early
        # so the database writer never encounters a late serialization error.
        import json

        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> "EventEnvelope":
        if self.parent_event_id == self.event_id:
            raise ValueError("parent_event_id cannot equal event_id")
        if (
            self.event_type in {EventType.SESSION_STARTED, EventType.SESSION_ENDED}
            and not self.session_id
        ):
            raise ValueError(f"{self.event_type.value} requires session_id")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        """Return JSON-compatible data with deterministic key ordering handled by caller."""

        return self.model_dump(mode="json", exclude_none=False)
