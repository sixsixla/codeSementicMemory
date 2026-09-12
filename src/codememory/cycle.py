"""Incremental agent-memory cycle orchestration.

This layer turns provider-specific lifecycle callbacks into the same durable
events used by the manual bridge.  It intentionally keeps the policy light:
route cards are useful hypotheses, feedback is idempotent evidence, and
extraction/consolidation remain rebuildable projections.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, model_validator

from .agent_bridge import (
    AgentBridgeService,
    CaptureRequest,
    FinishRequest,
    MemoryQueryRequest,
    SessionStartRequest,
    _BridgeModel,
)
from .extraction.providers import provider_from_name
from .extraction.context import ContextAssembler
from .extraction.models import Binding, Candidate, CandidateKind, ExtractionBatch, ExtractorInfo
from .extraction.service import ExtractionService
from .storage.repository import MemoryRepository


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _piece(value: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in str(value).strip())
    return cleaned.strip("-")[:180] or fallback


class CycleBaseRequest(_BridgeModel):
    project_id: str = Field(min_length=1, max_length=300)
    source_thread_id: str = Field(min_length=1, max_length=500)
    task_id: str | None = Field(default=None, max_length=300)
    session_id: str | None = Field(default=None, max_length=300)
    agent_id: str = Field(default="codex", min_length=1, max_length=200)
    adapter: str = Field(default="codex-hook", min_length=1, max_length=100)
    adapter_version: str = Field(default="0.1", min_length=1, max_length=100)
    context: dict[str, Any] = Field(default_factory=dict)


class CycleOpenRequest(CycleBaseRequest):
    prompt: str | None = Field(default=None, max_length=20_000)
    turn_id: str | None = Field(default=None, max_length=500)
    retrieval_mode: Literal["route", "trusted", "audit"] = "route"
    limit: int = Field(default=8, ge=1, le=50)


class CyclePromptRequest(CycleBaseRequest):
    prompt: str = Field(min_length=1, max_length=20_000)
    turn_id: str = Field(min_length=1, max_length=500)
    retrieval_mode: Literal["route", "trusted", "audit"] = "route"
    limit: int = Field(default=8, ge=1, le=50)


class CycleCheckpointRequest(CycleBaseRequest):
    turn_id: str = Field(min_length=1, max_length=500)
    intent: str | None = Field(default=None, max_length=20_000)
    explored_files: list[str] = Field(default_factory=list, max_length=500)
    modified_files: list[str] = Field(default_factory=list, max_length=500)
    symbols: list[str] = Field(default_factory=list, max_length=500)
    validations: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    rejected_candidates: list[str] = Field(default_factory=list, max_length=200)
    summary: str | None = Field(default=None, max_length=20_000)
    outcome: Literal["success", "failed", "partial", "unknown"] = "unknown"
    used_card_ids: list[str] = Field(default_factory=list, max_length=200)
    extract: bool = True
    consolidate: bool = True
    provider: str = Field(default="mock", min_length=1, max_length=100)
    force: bool = False

    @model_validator(mode="after")
    def evidence_must_not_be_empty(self) -> "CycleCheckpointRequest":
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
            raise ValueError("checkpoint evidence must contain at least one observable field")
        return self


class CycleCloseRequest(CycleBaseRequest):
    summary: str | None = Field(default=None, max_length=20_000)
    outcome: Literal["success", "failed", "partial", "unknown"] = "unknown"
    used_card_ids: list[str] = Field(default_factory=list, max_length=200)
    extract: bool = True
    consolidate: bool = True
    provider: str = Field(default="mock", min_length=1, max_length=100)
    force: bool = False


class AgentMemoryNote(_BridgeModel):
    kind: CandidateKind = CandidateKind.ROUTE_OBSERVATION
    statement: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    bindings: list[Binding] = Field(default_factory=list, max_length=200)
    evidence_event_ids: list[str] = Field(min_length=1, max_length=500)
    confidence: float = Field(default=0.65, ge=0, le=1)
    uncertainty: str = Field(
        default="Historical coding observation; verify current source.",
        min_length=1,
        max_length=2000,
    )
    relation_hints: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class CycleLearnRequest(CycleBaseRequest):
    turn_id: str = Field(min_length=1, max_length=500)
    input_hash: str = Field(min_length=1, max_length=100)
    model: str = Field(default="current-codex", min_length=1, max_length=200)
    notes: list[AgentMemoryNote] = Field(default_factory=list, max_length=20)
    used_card_ids: list[str] = Field(default_factory=list, max_length=200)
    outcome: Literal["success", "failed", "partial", "unknown"] = "unknown"


class _AgentSubmittedProvider:
    """Accept the current agent's LLM output, not a second paid model call."""

    provider_name = "agent-llm"
    prompt_version = "codememory-agent-learn-v1"

    def __init__(self, request: CycleLearnRequest) -> None:
        self.request = request
        self.model_name = request.model

    def extract(self, context: Any) -> ExtractionBatch:
        if context.input_hash != self.request.input_hash:
            raise ValueError("source evidence changed; run cycle prepare again before learning")
        candidates = [
            Candidate(
                candidate_id=f"agent-note-{_digest((context.task_id, note.model_dump(mode='json')))}",
                **note.model_dump(mode="python"),
            )
            for note in self.request.notes
        ]
        return ExtractionBatch(
            extraction_run_id=context.extraction_run_id,
            project_id=context.project_id,
            task_id=context.task_id,
            session_id=context.session_id,
            source_event_ids=list(context.event_ids),
            extractor=ExtractorInfo(
                provider=self.provider_name,
                model=self.model_name,
                prompt_version=self.prompt_version,
            ),
            candidates=candidates,
        )


class AgentMemoryCycleService:
    """Make Codex lifecycle events incremental, idempotent, and queryable."""

    def __init__(
        self, repository: MemoryRepository, *, bridge: AgentBridgeService | None = None
    ) -> None:
        self.repository = repository
        self.bridge = bridge or AgentBridgeService(repository)

    @staticmethod
    def _ids(request: CycleBaseRequest) -> tuple[str, str]:
        source = _piece(request.source_thread_id, "thread")
        task_id = request.task_id or f"codex-thread:{source}"
        session_id = request.session_id or f"codex-session:{source}"
        return task_id, session_id

    @staticmethod
    def _cycle_id(request: CycleBaseRequest, task_id: str, session_id: str) -> str:
        return (
            f"cycle-{_digest((request.project_id, request.source_thread_id, task_id, session_id))}"
        )

    @staticmethod
    def _last_seq(repository: MemoryRepository, task_id: str, session_id: str) -> int:
        timeline = repository.timeline(task_id, session_id=session_id, limit=5000)
        return max((int(item.get("seq", -1)) for item in timeline), default=-1)

    def _context(self, request: CycleBaseRequest) -> dict[str, Any]:
        context = dict(request.context)
        context.setdefault("source_system", "codex")
        context.setdefault("source_thread_id", request.source_thread_id)
        context.setdefault("agent_id", request.agent_id)
        context.setdefault("adapter", request.adapter)
        context.setdefault("adapter_version", request.adapter_version)
        return context

    def _ensure_started(self, request: CycleBaseRequest) -> tuple[str, str, str]:
        task_id, session_id = self._ids(request)
        start_event_id = self.bridge._event_id(session_id, "started")
        if self.repository.get_event(start_event_id) is None:
            self.bridge.start(
                SessionStartRequest(
                    project_id=request.project_id,
                    task_id=task_id,
                    session_id=session_id,
                    agent_id=request.agent_id,
                    adapter=request.adapter,
                    adapter_version=request.adapter_version,
                    title=f"Codex cycle {request.source_thread_id}",
                    context=self._context(request),
                )
            )
        return task_id, session_id, self._cycle_id(request, task_id, session_id)

    def _save(
        self,
        request: CycleBaseRequest,
        *,
        task_id: str,
        session_id: str,
        cycle_id: str,
        status: str,
        cursor: str | None,
        prompt_count: int,
        closed_at: str | None = None,
    ) -> dict[str, Any]:
        existing = self.repository.get_agent_memory_cycle(
            source_system="codex",
            source_thread_id=request.source_thread_id,
            project_id=request.project_id,
            session_id=session_id,
        )
        metadata = dict(existing.get("metadata") or {}) if existing else {}
        metadata.update(self._context(request))
        return self.repository.upsert_agent_memory_cycle(
            cycle_id=cycle_id,
            source_system="codex",
            source_thread_id=request.source_thread_id,
            project_id=request.project_id,
            task_id=task_id,
            session_id=session_id,
            status=status,
            cursor=cursor,
            prompt_count=prompt_count,
            last_event_seq=self._last_seq(self.repository, task_id, session_id),
            metadata=metadata,
            opened_at=(existing or {}).get("opened_at"),
            closed_at=closed_at,
        )

    def _record_cards(
        self,
        request: CycleBaseRequest,
        *,
        task_id: str,
        session_id: str,
        turn_id: str,
        card_ids: list[str],
        feedback_type: str,
        outcome: str = "unknown",
        used: bool = False,
    ) -> int:
        count = 0
        for card_id in dict.fromkeys(card_ids):
            key = _digest((task_id, session_id, turn_id, card_id, feedback_type))
            try:
                if self.repository.record_memory_card_feedback(
                    feedback_id=f"feedback-{key}",
                    feedback_key=key,
                    project_id=request.project_id,
                    task_id=task_id,
                    session_id=session_id,
                    card_id=card_id,
                    turn_id=turn_id,
                    feedback_type=feedback_type,
                    outcome=outcome,
                    used=used,
                    metadata=self._context(request),
                ):
                    count += 1
            except Exception:
                # A stale card must never prevent the coding turn from closing.
                continue
        return count

    def _query(
        self, request: CycleBaseRequest, query: str | None, *, limit: int, retrieval_mode: str
    ) -> dict[str, Any]:
        if not query:
            return {"cards": [], "events": [], "query": "", "retrieval_mode": retrieval_mode}
        return self.bridge.query(
            MemoryQueryRequest(
                query=query,
                project_id=request.project_id,
                task_id=request.task_id,
                limit=limit,
                retrieval_mode=retrieval_mode,  # type: ignore[arg-type]
            )
        )

    def open(self, request: CycleOpenRequest) -> dict[str, Any]:
        task_id, session_id, cycle_id = self._ensure_started(request)
        prompt_count = 0
        prompt_result: dict[str, Any] | None = None
        turn_id = request.turn_id or f"open-{_digest(request.prompt or request.source_thread_id)}"
        if request.prompt:
            prompt_result = self.prompt(
                CyclePromptRequest(
                    **request.model_dump(exclude={"prompt", "turn_id", "retrieval_mode", "limit"}),
                    prompt=request.prompt,
                    turn_id=turn_id,
                    retrieval_mode=request.retrieval_mode,
                    limit=request.limit,
                )
            )
            prompt_count = int((prompt_result.get("cycle") or {}).get("prompt_count", 1))
        else:
            existing = self.repository.get_agent_memory_cycle(
                source_system="codex",
                source_thread_id=request.source_thread_id,
                project_id=request.project_id,
                session_id=session_id,
            )
            prompt_count = int((existing or {}).get("prompt_count", 0))
        query = self._query(
            request, request.prompt, limit=request.limit, retrieval_mode=request.retrieval_mode
        )
        if request.prompt:
            cycle = prompt_result["cycle"] if prompt_result else {}
        else:
            cycle = self._save(
                request,
                task_id=task_id,
                session_id=session_id,
                cycle_id=cycle_id,
                status="active",
                cursor=None,
                prompt_count=prompt_count,
            )
        return {"cycle": cycle, "task_id": task_id, "session_id": session_id, "query": query}

    def prompt(self, request: CyclePromptRequest) -> dict[str, Any]:
        task_id, session_id, cycle_id = self._ensure_started(request)
        existing = self.repository.get_agent_memory_cycle(
            source_system="codex",
            source_thread_id=request.source_thread_id,
            project_id=request.project_id,
            session_id=session_id,
        )
        prompt_count = int((existing or {}).get("prompt_count", 0))
        existing_prompt = any(
            (item.get("payload") or {}).get("capture_id") == f"prompt:{request.turn_id}"
            for item in self.repository.timeline(task_id, session_id=session_id, limit=5000)
        )
        self.bridge.capture(
            CaptureRequest(
                project_id=request.project_id,
                task_id=task_id,
                session_id=session_id,
                capture_id=f"prompt:{request.turn_id}",
                intent=request.prompt,
                outcome="partial",
                context=self._context(request),
            )
        )
        query = self._query(
            request, request.prompt, limit=request.limit, retrieval_mode=request.retrieval_mode
        )
        card_ids = [
            str(card.get("card_id")) for card in query.get("cards", []) if card.get("card_id")
        ]
        self._record_cards(
            request,
            task_id=task_id,
            session_id=session_id,
            turn_id=request.turn_id,
            card_ids=card_ids,
            feedback_type="presented",
        )
        cycle = self._save(
            request,
            task_id=task_id,
            session_id=session_id,
            cycle_id=cycle_id,
            status="active",
            cursor=request.turn_id,
            prompt_count=prompt_count if existing_prompt else prompt_count + 1,
        )
        return {"cycle": cycle, "task_id": task_id, "session_id": session_id, "query": query}

    def checkpoint(self, request: CycleCheckpointRequest) -> dict[str, Any]:
        task_id, session_id, cycle_id = self._ensure_started(request)
        capture = self.bridge.capture(
            CaptureRequest(
                project_id=request.project_id,
                task_id=task_id,
                session_id=session_id,
                capture_id=f"checkpoint:{request.turn_id}",
                intent=request.intent,
                explored_files=request.explored_files,
                modified_files=request.modified_files,
                symbols=request.symbols,
                validations=request.validations,
                rejected_candidates=request.rejected_candidates,
                summary=request.summary,
                outcome=request.outcome,
                context=self._context(request),
            )
        )
        output: dict[str, Any] = {"capture": capture}
        if request.extract:
            provider = provider_from_name(request.provider)
            extraction = self.bridge.extraction_service.__class__(
                self.repository,
                store=self.bridge.extraction_store,
                provider=provider,
                quality_service=self.bridge.quality_service,
            )
            output["extraction"] = extraction.extract_task(
                task_id, session_id=session_id, force=request.force
            ).as_dict()
            if request.consolidate:
                output["consolidation"] = self.bridge.consolidation_service.consolidate(
                    task_id=task_id
                ).as_dict()
            output["completed_outbox_jobs"] = self.repository.complete_outbox_for_task(task_id)
        feedback = self._record_cards(
            request,
            task_id=task_id,
            session_id=session_id,
            turn_id=request.turn_id,
            card_ids=request.used_card_ids,
            feedback_type="outcome",
            outcome=request.outcome,
            used=True,
        )
        output["feedback_recorded"] = feedback
        existing = self.repository.get_agent_memory_cycle(
            source_system="codex",
            source_thread_id=request.source_thread_id,
            project_id=request.project_id,
            session_id=session_id,
        )
        output["cycle"] = self._save(
            request,
            task_id=task_id,
            session_id=session_id,
            cycle_id=cycle_id,
            status="checkpointed",
            cursor=request.turn_id,
            prompt_count=int((existing or {}).get("prompt_count", 0)),
        )
        return output

    def close(self, request: CycleCloseRequest) -> dict[str, Any]:
        task_id, session_id, cycle_id = self._ensure_started(request)
        result = self.bridge.finish(
            FinishRequest(
                project_id=request.project_id,
                task_id=task_id,
                session_id=session_id,
                agent_id=request.agent_id,
                adapter=request.adapter,
                adapter_version=request.adapter_version,
                summary=request.summary,
                outcome=request.outcome,
                context=self._context(request),
                extract=request.extract,
                consolidate=request.consolidate,
                provider=request.provider,
                force=request.force,
            )
        )
        feedback = self._record_cards(
            request,
            task_id=task_id,
            session_id=session_id,
            turn_id="close",
            card_ids=request.used_card_ids,
            feedback_type="outcome",
            outcome=request.outcome,
            used=True,
        )
        existing = self.repository.get_agent_memory_cycle(
            source_system="codex",
            source_thread_id=request.source_thread_id,
            project_id=request.project_id,
            session_id=session_id,
        )
        result["feedback_recorded"] = feedback
        result["cycle"] = self._save(
            request,
            task_id=task_id,
            session_id=session_id,
            cycle_id=cycle_id,
            status="closed",
            cursor="close",
            prompt_count=int((existing or {}).get("prompt_count", 0)),
            closed_at=_now().isoformat(),
        )
        return result

    def prepare(self, request: CycleBaseRequest) -> dict[str, Any]:
        """Expose a bounded, evidence-addressed packet for the current Codex LLM."""
        task_id, session_id, cycle_id = self._ensure_started(request)
        context = ContextAssembler(
            self.repository,
            quality_service=self.bridge.quality_service,
            max_events=150,
            max_chars=45_000,
            max_event_chars=4000,
        ).assemble(task_id, session_id=session_id)
        return {
            "cycle_id": cycle_id,
            "request": {
                **request.model_dump(mode="json"),
                "task_id": task_id,
                "session_id": session_id,
            },
            "input_hash": context.input_hash,
            "events": list(context.events),
            "truncated": context.truncated,
            "maintenance": self.repository.latest_agent_memory_maintenance(cycle_id=cycle_id),
            "instruction": "Submit up to 8 useful coding notes with exact evidence_event_ids and binding.evidence. Empty notes are valid. Do not invent success or current-code verification.",
            "learn_schema": {
                "project_id": request.project_id,
                "source_thread_id": request.source_thread_id,
                "task_id": task_id,
                "session_id": session_id,
                "turn_id": "<maintenance-turn-id>",
                "input_hash": context.input_hash,
                "model": "<current-codex-model>",
                "outcome": "unknown",
                "notes": [
                    {
                        "kind": "route_observation",
                        "statement": "<one useful coding fact>",
                        "aliases": [],
                        "evidence_event_ids": ["<event_id from events>"],
                        "bindings": [
                            {
                                "role": "modified_file",
                                "path": "<path>",
                                "symbol": None,
                                "qualified_symbol": None,
                                "evidence": ["<same event_id>"],
                            }
                        ],
                        "confidence": 0.65,
                        "uncertainty": "Historical observation; verify current source.",
                        "relation_hints": [],
                    }
                ],
            },
        }

    def request_maintenance(
        self,
        request: CycleBaseRequest,
        *,
        input_hash: str,
        turn_id: str,
        max_attempts: int = 2,
        provider: str = "agent",
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one bounded current-agent maintenance continuation."""

        task_id, session_id, cycle_id = self._ensure_started(request)
        maintenance_id = f"maintenance-{_digest((cycle_id, input_hash))}"
        return self.repository.request_agent_memory_maintenance(
            maintenance_id=maintenance_id,
            cycle_id=cycle_id,
            project_id=request.project_id,
            task_id=task_id,
            session_id=session_id,
            source_thread_id=request.source_thread_id,
            request_turn_id=turn_id,
            input_hash=input_hash,
            max_attempts=max_attempts,
            provider=provider,
            model=model,
            metadata=metadata,
        )

    def fail_pending_maintenance(
        self, request: CycleBaseRequest, *, error: str
    ) -> dict[str, Any] | None:
        """Mark an unfinished continuation so a later coding turn can retry it."""

        _, _, cycle_id = self._ensure_started(request)
        return self.repository.fail_pending_agent_memory_maintenance(
            cycle_id=cycle_id,
            error=error,
        )

    def learn(self, request: CycleLearnRequest) -> dict[str, Any]:
        """Validate current-agent LLM notes through the existing extraction pipeline."""
        task_id, session_id, cycle_id = self._ensure_started(request)
        try:
            assembler = ContextAssembler(
                self.repository,
                quality_service=self.bridge.quality_service,
                max_events=150,
                max_chars=45_000,
                max_event_chars=4000,
            )
            current = assembler.assemble(task_id, session_id=session_id)
            if current.input_hash != request.input_hash:
                raise ValueError("source evidence changed; run cycle prepare again before learning")
            extraction = ExtractionService(
                self.repository,
                assembler=assembler,
                store=self.bridge.extraction_store,
                provider=_AgentSubmittedProvider(request),
                quality_service=self.bridge.quality_service,
                extractor_version="agent-memory-submit-v1",
            ).extract_task(task_id, session_id=session_id)
            output: dict[str, Any] = {"extraction": extraction.as_dict()}
            if extraction.status in {"extracted", "duplicate"}:
                output["consolidation"] = self.bridge.consolidation_service.consolidate(
                    task_id=task_id
                ).as_dict()
                output["completed_outbox_jobs"] = self.repository.complete_outbox_for_task(task_id)
                output["feedback_recorded"] = self._record_cards(
                    request,
                    task_id=task_id,
                    session_id=session_id,
                    turn_id=request.turn_id,
                    card_ids=request.used_card_ids,
                    feedback_type="outcome",
                    outcome=request.outcome,
                    used=True,
                )
                existing = self.repository.get_agent_memory_cycle(
                    source_system="codex",
                    source_thread_id=request.source_thread_id,
                    project_id=request.project_id,
                    session_id=session_id,
                )
                output["cycle"] = self._save(
                    request,
                    task_id=task_id,
                    session_id=session_id,
                    cycle_id=cycle_id,
                    status="checkpointed",
                    cursor=request.turn_id,
                    prompt_count=int((existing or {}).get("prompt_count", 0)),
                )
                output["maintenance"] = self.repository.complete_agent_memory_maintenance(
                    cycle_id=cycle_id,
                    input_hash=request.input_hash,
                    model=request.model,
                    note_count=len(request.notes),
                    candidate_count=int(extraction.candidate_count),
                )
            else:
                output["maintenance"] = self.repository.fail_agent_memory_maintenance(
                    cycle_id=cycle_id,
                    input_hash=request.input_hash,
                    error=f"extraction ended with status {extraction.status}",
                )
            return output
        except Exception as exc:
            self.repository.fail_agent_memory_maintenance(
                cycle_id=cycle_id,
                input_hash=request.input_hash,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
