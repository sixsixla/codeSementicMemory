"""Manual-first lifecycle bridge for coding agents.

The bridge deliberately sits above the canonical event service.  It gives an
agent a small, stable surface (start/query/capture/finish) without requiring a
provider-specific hook or access to a desktop application's private state.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .consolidation.service import ConsolidationService
from .consolidation.store import CardStore
from .domain.events import EventCompleteness, EventEnvelope, EventType
from .extraction.providers import provider_from_name
from .extraction.service import ExtractionService
from .extraction.store import ExtractionStore
from .ingest.redaction import redact_event
from .ingest.service import IngestService
from .quality.service import QualityService
from .storage.repository import ConflictError, MemoryRepository


AGENT_BRIDGE_SCHEMA_VERSION = "codememory.agent_bridge.v1"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._:-]+")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _stable_piece(value: str, *, fallback: str) -> str:
    value = _SLUG_RE.sub("-", str(value).strip()).strip("-")
    return value[:180] or fallback


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


class _BridgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SessionStartRequest(_BridgeModel):
    project_id: str = Field(min_length=1, max_length=300)
    task_id: str | None = Field(default=None, max_length=300)
    session_id: str | None = Field(default=None, max_length=300)
    agent_id: str = Field(default="codex", min_length=1, max_length=200)
    adapter: str = Field(default="manual-skill", min_length=1, max_length=100)
    adapter_version: str = Field(default="0.1", min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=500)
    intent: str | None = Field(default=None, max_length=20_000)
    context: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class MemoryQueryRequest(_BridgeModel):
    query: str = Field(min_length=1, max_length=500)
    project_id: str | None = Field(default=None, max_length=300)
    task_id: str | None = Field(default=None, max_length=300)
    limit: int = Field(default=20, ge=1, le=200)
    retrieval_mode: Literal["route", "trusted", "audit"] = "route"
    include_quarantine: bool = False


class CaptureRequest(_BridgeModel):
    project_id: str = Field(min_length=1, max_length=300)
    task_id: str = Field(min_length=1, max_length=300)
    session_id: str = Field(min_length=1, max_length=300)
    capture_id: str | None = Field(default=None, max_length=300)
    intent: str | None = Field(default=None, max_length=20_000)
    explored_files: list[str] = Field(default_factory=list, max_length=500)
    modified_files: list[str] = Field(default_factory=list, max_length=500)
    symbols: list[str] = Field(default_factory=list, max_length=500)
    validations: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    rejected_candidates: list[str] = Field(default_factory=list, max_length=200)
    summary: str | None = Field(default=None, max_length=20_000)
    outcome: Literal["success", "failed", "partial", "unknown"] = "partial"
    context: dict[str, Any] = Field(default_factory=dict)
    completeness: EventCompleteness = EventCompleteness.SUMMARY
    captured_at: datetime | None = None

    @model_validator(mode="after")
    def evidence_must_not_be_empty(self) -> "CaptureRequest":
        if not any(
            (
                self.intent,
                self.explored_files,
                self.modified_files,
                self.symbols,
                self.validations,
                self.rejected_candidates,
                self.summary,
            )
        ):
            raise ValueError("capture evidence must contain intent, files, symbols, validation, or summary")
        return self


class FinishRequest(_BridgeModel):
    project_id: str = Field(min_length=1, max_length=300)
    task_id: str = Field(min_length=1, max_length=300)
    session_id: str = Field(min_length=1, max_length=300)
    agent_id: str = Field(default="codex", min_length=1, max_length=200)
    adapter: str = Field(default="manual-skill", min_length=1, max_length=100)
    adapter_version: str = Field(default="0.1", min_length=1, max_length=100)
    summary: str | None = Field(default=None, max_length=20_000)
    outcome: Literal["success", "failed", "partial", "unknown"] = "partial"
    context: dict[str, Any] = Field(default_factory=dict)
    completeness: EventCompleteness = EventCompleteness.SUMMARY
    occurred_at: datetime | None = None
    extract: bool = True
    consolidate: bool = True
    provider: str = Field(default="mock", min_length=1, max_length=100)
    force: bool = False


class AgentBridgeService:
    """Translate coarse manual lifecycle calls into canonical coding events."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        ingest_service: IngestService | None = None,
        card_store: CardStore | None = None,
        extraction_service: ExtractionService | None = None,
        consolidation_service: ConsolidationService | None = None,
        quality_service: QualityService | None = None,
    ) -> None:
        self.repository = repository
        self.quality_service = quality_service or QualityService(repository)
        self.ingest_service = ingest_service or IngestService(
            repository, quality_service=self.quality_service
        )
        self.card_store = card_store or CardStore(repository.db)
        self.extraction_store = ExtractionStore(repository.db)
        self.extraction_service = extraction_service or ExtractionService(
            repository,
            store=self.extraction_store,
            quality_service=self.quality_service,
        )
        self.consolidation_service = consolidation_service or ConsolidationService(
            repository,
            store=self.card_store,
            quality_service=self.quality_service,
        )

    @staticmethod
    def _ids(request: SessionStartRequest) -> tuple[str, str]:
        session_id = request.session_id or f"session-{uuid.uuid4().hex}"
        task_id = request.task_id or f"task-{_stable_piece(session_id, fallback=uuid.uuid4().hex)}"
        return task_id, session_id

    @staticmethod
    def _event_id(session_id: str, suffix: str) -> str:
        session_piece = _stable_piece(session_id, fallback="session")[:96]
        suffix_piece = _stable_piece(suffix, fallback="event")
        if len(suffix_piece) > 160:
            suffix_piece = f"{suffix_piece[:128]}-{_digest(suffix_piece)}"
        return f"agent-bridge-{session_piece}-{suffix_piece}"[:300]

    @staticmethod
    def _comparable(event: EventEnvelope) -> dict[str, Any]:
        data = event.canonical_dict()
        # Redaction metadata is allowed to be filled by the ingest boundary.
        # occurred_at is restored from the existing event for idempotent retry.
        data.pop("redaction", None)
        data.pop("occurred_at", None)
        return data

    def _idempotent(self, event: EventEnvelope) -> EventEnvelope:
        existing_raw = self.repository.get_event(event.event_id)
        if existing_raw is None:
            return redact_event(event)
        existing_payload = dict(existing_raw)
        existing_payload.pop("ingested_at", None)
        existing = EventEnvelope.model_validate(existing_payload)
        candidate = event.model_copy(update={"occurred_at": existing.occurred_at})
        normalized = redact_event(candidate)
        if self._comparable(existing) != self._comparable(normalized):
            raise ConflictError(
                "agent bridge identity was reused with different content",
                existing_event_id=event.event_id,
            )
        return existing

    def _context(self, request_context: dict[str, Any], timeline: list[dict[str, Any]]) -> dict[str, Any]:
        context: dict[str, Any] = {}
        for item in reversed(timeline):
            value = item.get("context")
            if isinstance(value, dict):
                context.update(value)
                break
        context.update(request_context)
        return context

    def _next_seq(self, task_id: str, session_id: str) -> tuple[int, str | None, dict[str, Any]]:
        timeline = self.repository.timeline(task_id, session_id=session_id, limit=5000)
        if not timeline:
            return 0, None, {}
        last = max(timeline, key=lambda item: int(item.get("seq", 0)))
        return int(last.get("seq", 0)) + 1, str(last.get("event_id")), dict(last.get("context") or {})

    def _build_event(
        self,
        *,
        event_id: str,
        event_type: EventType,
        project_id: str,
        task_id: str,
        session_id: str,
        seq: int,
        parent_event_id: str | None,
        producer: dict[str, str],
        payload: dict[str, Any],
        context: dict[str, Any],
        completeness: EventCompleteness,
        occurred_at: datetime | None,
    ) -> EventEnvelope:
        return EventEnvelope.model_validate(
            {
                "schema_version": "codememory.event.v1",
                "event_id": event_id,
                "external_event_id": event_id,
                "event_type": event_type.value,
                "occurred_at": occurred_at or _now(),
                "producer": producer,
                "project_id": project_id,
                "task_id": task_id,
                "session_id": session_id,
                "seq": seq,
                "parent_event_id": parent_event_id,
                "context": context,
                "payload": {
                    "bridge_schema_version": AGENT_BRIDGE_SCHEMA_VERSION,
                    **payload,
                },
                "artifacts": [],
                "redaction": {"applied": False, "ruleset_version": "builtin-v1", "fields": []},
                "source": "agent",
                "completeness": completeness.value,
            }
        )

    def start(self, request: SessionStartRequest) -> dict[str, Any]:
        task_id, session_id = self._ids(request)
        event = self._build_event(
            event_id=self._event_id(session_id, "started"),
            event_type=EventType.SESSION_STARTED,
            project_id=request.project_id,
            task_id=task_id,
            session_id=session_id,
            seq=0,
            parent_event_id=None,
            producer={
                "agent_id": request.agent_id,
                "adapter": request.adapter,
                "adapter_version": request.adapter_version,
            },
            payload={
                "title": request.title,
                "text": request.intent,
                "manual_capture": True,
            },
            context=request.context,
            completeness=EventCompleteness.FULL,
            occurred_at=request.occurred_at,
        )
        event = self._idempotent(event)
        result = self.ingest_service.ingest(event)
        return {
            "schema_version": AGENT_BRIDGE_SCHEMA_VERSION,
            "task_id": task_id,
            "session_id": session_id,
            "event": result.as_dict(),
            "next": ["query", "capture", "finish"],
        }

    def query(self, request: MemoryQueryRequest) -> dict[str, Any]:
        cards = self.card_store.search(
            request.query,
            project_id=request.project_id,
            task_id=request.task_id,
            limit=request.limit,
            retrieval_mode=request.retrieval_mode,
            include_quarantine=request.include_quarantine,
        )
        events = self.repository.search(
            request.query,
            project_id=request.project_id,
            limit=request.limit,
        )
        if request.task_id:
            events = [item for item in events if item.get("task_id") == request.task_id]
        return {
            "schema_version": AGENT_BRIDGE_SCHEMA_VERSION,
            "query": request.query,
            "retrieval_mode": request.retrieval_mode,
            "cards": cards,
            "events": events,
        }

    def capture(self, request: CaptureRequest) -> dict[str, Any]:
        evidence = {
            "intent": request.intent,
            "explored_files": request.explored_files,
            "modified_files": request.modified_files,
            "symbols": request.symbols,
            "validations": request.validations,
            "rejected_candidates": request.rejected_candidates,
            "summary": request.summary,
            "outcome": request.outcome,
        }
        capture_id = request.capture_id or f"capture-{_digest(evidence)}"
        timeline = self.repository.timeline(request.task_id, session_id=request.session_id, limit=5000)
        existing_capture = [
            item for item in timeline if (item.get("payload") or {}).get("capture_id") == capture_id
        ]
        if existing_capture:
            first_existing = min(existing_capture, key=lambda item: int(item.get("seq", 0)))
            next_seq = int(first_existing.get("seq", 0))
            parent_event_id = first_existing.get("parent_event_id")
            previous_context = dict(first_existing.get("context") or {})
            captured_at = datetime.fromisoformat(
                str(first_existing["occurred_at"]).replace("Z", "+00:00")
            )
        else:
            next_seq, parent_event_id, previous_context = self._next_seq(
                request.task_id, request.session_id
            )
            captured_at = request.captured_at
        context = self._context(request.context, [{"context": previous_context}])
        producer = {
            "agent_id": str(request.context.get("agent_id") or "codex"),
            "adapter": str(request.context.get("adapter") or "manual-skill"),
            "adapter_version": str(request.context.get("adapter_version") or "0.1"),
        }
        common = {
            "project_id": request.project_id,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "context": context,
            "completeness": request.completeness,
            "occurred_at": captured_at,
            "producer": producer,
        }
        events: list[EventEnvelope] = []

        def add(event_type: EventType, payload: dict[str, Any], suffix: str) -> None:
            nonlocal next_seq, parent_event_id
            event = self._build_event(
                event_id=self._event_id(request.session_id, f"{capture_id}-{suffix}"),
                event_type=event_type,
                seq=next_seq,
                parent_event_id=parent_event_id,
                payload={"capture_id": capture_id, **payload},
                **common,
            )
            prepared = self._idempotent(event)
            events.append(prepared)
            parent_event_id = prepared.event_id
            next_seq += 1

        if request.intent:
            add(EventType.USER_MESSAGE, {"text": request.intent}, "intent")
        for index, path in enumerate(request.explored_files):
            add(EventType.FILE_READ, {"path": path, "role": "explored"}, f"read-{index}")
        for index, path in enumerate(request.modified_files):
            add(EventType.FILE_EDIT, {"path": path, "role": "modified"}, f"edit-{index}")
        if request.symbols or request.rejected_candidates:
            add(
                EventType.TOOL_RESULT,
                {
                    "symbols": request.symbols,
                    "rejected_candidates": request.rejected_candidates,
                },
                "symbols",
            )
        for index, validation in enumerate(request.validations):
            add(EventType.VALIDATION_RUN, dict(validation), f"validation-{index}")
        if request.summary or request.outcome != "partial":
            add(
                EventType.ASSISTANT_MESSAGE,
                {"text": request.summary, "outcome": request.outcome},
                "summary",
            )
        if existing_capture and {item.event_id for item in events} != {
            str(item.get("event_id")) for item in existing_capture
        }:
            raise ConflictError(
                "capture_id was reused with a different event shape",
                existing_event_id=str(existing_capture[0].get("event_id")),
            )
        results = self.ingest_service.ingest_many(events, atomic=True)
        return {
            "schema_version": AGENT_BRIDGE_SCHEMA_VERSION,
            "capture_id": capture_id,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "event_count": len(events),
            "accepted": sum(item.status == "accepted" for item in results),
            "duplicates": sum(item.status == "duplicate" for item in results),
            "events": [item.as_dict() for item in results],
        }

    def finish(self, request: FinishRequest) -> dict[str, Any]:
        next_seq, parent_event_id, previous_context = self._next_seq(
            request.task_id, request.session_id
        )
        finished_event_id = self._event_id(request.session_id, "finished")
        existing_raw = self.repository.get_event(finished_event_id)
        if existing_raw is not None:
            existing_payload = dict(existing_raw)
            existing_payload.pop("ingested_at", None)
            existing = EventEnvelope.model_validate(existing_payload)
            next_seq = existing.seq
            parent_event_id = existing.parent_event_id
            previous_context = existing.context.model_dump(mode="python")
            occurred_at = existing.occurred_at
        else:
            occurred_at = request.occurred_at
        event = self._build_event(
            event_id=finished_event_id,
            event_type=EventType.SESSION_ENDED,
            project_id=request.project_id,
            task_id=request.task_id,
            session_id=request.session_id,
            seq=next_seq,
            parent_event_id=parent_event_id,
            producer={
                "agent_id": request.agent_id,
                "adapter": request.adapter,
                "adapter_version": request.adapter_version,
            },
            payload={
                "text": request.summary,
                "outcome": request.outcome,
                "manual_capture": True,
            },
            context=self._context(request.context, [{"context": previous_context}]),
            completeness=request.completeness,
            occurred_at=occurred_at,
        )
        event = self._idempotent(event)
        ingest_result = self.ingest_service.ingest(event)
        output: dict[str, Any] = {
            "schema_version": AGENT_BRIDGE_SCHEMA_VERSION,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "event": ingest_result.as_dict(),
        }
        if request.extract:
            provider = provider_from_name(request.provider)
            extraction = ExtractionService(
                self.repository,
                store=self.extraction_store,
                provider=provider,
                quality_service=self.quality_service,
            )
            output["extraction"] = extraction.extract_task(
                request.task_id,
                session_id=request.session_id,
                force=request.force,
            ).as_dict()
            if request.consolidate:
                output["consolidation"] = self.consolidation_service.consolidate(
                    task_id=request.task_id
                ).as_dict()
            output["completed_outbox_jobs"] = self.repository.complete_outbox_for_task(
                request.task_id
            )
        return output
