"""Thin producer adapters.

Adapters only normalize native input and attach provenance. They never call an
LLM, decide memory-card semantics, or write SQLite directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from .domain.events import EventEnvelope, EventType


class AgentEventAdapter(Protocol):
    def normalize(self, raw_event: Any) -> EventEnvelope:
        """Translate one native event into the stable envelope."""


class FixtureAdapter:
    """Adapter used by JSONL fixtures and deterministic tests."""

    def normalize(self, raw_event: Any) -> EventEnvelope:
        if isinstance(raw_event, EventEnvelope):
            return raw_event
        return EventEnvelope.model_validate(raw_event)


class CliProducer:
    """Build one event for local smoke tests and manual capture."""

    def __init__(self, *, agent_id: str = "cli", adapter_version: str = "0.1"):
        self.agent_id = agent_id
        self.adapter_version = adapter_version

    def event(
        self,
        *,
        event_type: EventType,
        project_id: str,
        task_id: str,
        session_id: str | None,
        seq: int,
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        return EventEnvelope.model_validate(
            {
                "schema_version": "codememory.event.v1",
                "event_id": f"cli-{uuid.uuid4()}",
                "event_type": event_type.value,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "producer": {
                    "agent_id": self.agent_id,
                    "adapter": "cli",
                    "adapter_version": self.adapter_version,
                },
                "project_id": project_id,
                "task_id": task_id,
                "session_id": session_id,
                "seq": seq,
                "context": context or {},
                "payload": payload,
                "artifacts": [],
                "redaction": {"applied": False, "ruleset_version": "builtin-v1", "fields": []},
                "source": "api",
                "completeness": "full",
            }
        )
