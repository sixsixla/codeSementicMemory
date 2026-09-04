"""Orchestration for bounded, auditable Phase 2A extraction."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import ValidationError

from ..storage.repository import MemoryRepository
from ..quality.service import QualityService
from .context import ContextAssembler
from .models import EXTRACTION_SCHEMA_VERSION, ExtractionBatch
from .providers import ExtractionProvider, MockLLMProvider
from .store import ExtractionStore


class ExtractionValidationError(ValueError):
    """Provider output cannot be safely linked to the assembled evidence."""


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    run_id: str | None
    task_id: str
    input_hash: str | None
    candidate_count: int
    event_count: int
    error: str | None = None
    quality_decisions: dict[str, int] = field(default_factory=dict)
    quality_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExtractionService:
    """Keep provider work off the event-ingest ACK path."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        assembler: ContextAssembler | None = None,
        store: ExtractionStore | None = None,
        provider: ExtractionProvider | None = None,
        quality_service: QualityService | None = None,
        extractor_version: str = "phase2a-v1",
        schema_version: str = EXTRACTION_SCHEMA_VERSION,
    ) -> None:
        self.repository = repository
        self.quality_service = quality_service or QualityService(repository)
        self.assembler = assembler or ContextAssembler(
            repository, quality_service=self.quality_service
        )
        self.store = store or ExtractionStore(repository.db)
        self.provider = provider or MockLLMProvider()
        self.extractor_version = extractor_version
        self.schema_version = schema_version

    def extract_task(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        force: bool = False,
    ) -> ExtractionResult:
        context = self.assembler.assemble(task_id, session_id=session_id)
        provider_name = str(getattr(self.provider, "provider_name", self.provider.__class__.__name__))
        model_name = str(getattr(self.provider, "model_name", "unknown"))
        prompt_version = str(getattr(self.provider, "prompt_version", "unspecified"))
        run = self.store.begin_run(
            context,
            provider=provider_name,
            model=model_name,
            extractor_version=self.extractor_version,
            prompt_version=prompt_version,
            schema_version=self.schema_version,
            force=force,
        )
        if not run.execute:
            if run.status == "dead":
                status = "dead"
            elif run.status == "succeeded":
                status = "duplicate"
            else:
                status = "in_progress"
            return ExtractionResult(
                status=status,
                run_id=run.run_id,
                task_id=task_id,
                input_hash=run.input_hash,
                candidate_count=self._candidate_count(run.run_id),
                event_count=len(context.event_ids),
            )
        run_context = context.with_run_id(run.run_id)
        try:
            raw_batch = self.provider.extract(run_context)
            batch = raw_batch if isinstance(raw_batch, ExtractionBatch) else ExtractionBatch.model_validate(raw_batch)
            self._validate_batch(
                batch,
                run_context,
                provider_name=provider_name,
                model_name=model_name,
                prompt_version=prompt_version,
            )
            stored = self.store.record_success(run, run_context, batch)
            quality_decisions: dict[str, int] = {}
            quality_error: str | None = None
            try:
                candidates = self.store.list_candidates(
                    run_id=stored.run_id, limit=100_000, include_quarantine=True
                )
                reviews = self.quality_service.review_candidates(candidates)
                for review in reviews:
                    quality_decisions[review.decision] = quality_decisions.get(review.decision, 0) + 1
            except Exception as exc:  # quality is a rebuildable projection; keep extraction durable
                quality_error = f"{type(exc).__name__}: {exc}"
            return ExtractionResult(
                status="extracted",
                run_id=stored.run_id,
                task_id=task_id,
                input_hash=context.input_hash,
                candidate_count=stored.candidate_count,
                event_count=len(context.event_ids),
                quality_decisions=quality_decisions,
                quality_error=quality_error,
            )
        except (
            ValidationError,
            ExtractionValidationError,
            ValueError,
            TypeError,
            sqlite3.IntegrityError,
        ) as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.store.record_failure(run.run_id, message, dead=run.attempts >= 3)
            return ExtractionResult(
                status="failed",
                run_id=run.run_id,
                task_id=task_id,
                input_hash=context.input_hash,
                candidate_count=0,
                event_count=len(context.event_ids),
                error=message,
            )
        except Exception as exc:  # provider/network failures must not strand a run as running
            message = f"{type(exc).__name__}: {exc}"
            self.store.record_failure(run.run_id, message, dead=run.attempts >= 3)
            return ExtractionResult(
                status="failed",
                run_id=run.run_id,
                task_id=task_id,
                input_hash=context.input_hash,
                candidate_count=0,
                event_count=len(context.event_ids),
                error=message,
            )

    def _validate_batch(
        self,
        batch: ExtractionBatch,
        context: Any,
        *,
        provider_name: str,
        model_name: str,
        prompt_version: str,
    ) -> None:
        if batch.extraction_run_id != context.extraction_run_id:
            raise ExtractionValidationError("extraction_run_id does not match the leased run")
        if batch.project_id != context.project_id or batch.task_id != context.task_id:
            raise ExtractionValidationError("provider changed project_id or task_id")
        if batch.session_id != context.session_id:
            raise ExtractionValidationError("provider changed session_id")
        if (
            batch.extractor.provider != provider_name
            or batch.extractor.model != model_name
            or batch.extractor.prompt_version != prompt_version
        ):
            raise ExtractionValidationError(
                "provider metadata does not match the leased extraction run"
            )
        known = set(context.event_ids)
        source_ids = set(batch.source_event_ids)
        if not source_ids:
            raise ExtractionValidationError("source_event_ids cannot be empty")
        unknown_sources = source_ids - known
        if unknown_sources:
            raise ExtractionValidationError(
                f"source_event_ids reference unknown events: {sorted(unknown_sources)[:5]}"
            )
        candidate_ids: set[str] = set()
        for candidate in batch.candidates:
            if candidate.candidate_id in candidate_ids:
                raise ExtractionValidationError(f"duplicate candidate_id: {candidate.candidate_id}")
            candidate_ids.add(candidate.candidate_id)
            candidate_events = set(candidate.evidence_event_ids)
            unknown = candidate_events - known
            if unknown:
                raise ExtractionValidationError(
                    f"candidate {candidate.candidate_id} references unknown evidence: {sorted(unknown)[:5]}"
                )
            if not candidate_events:
                raise ExtractionValidationError(f"candidate {candidate.candidate_id} has no evidence")
            undeclared = candidate_events - source_ids
            if undeclared:
                raise ExtractionValidationError(
                    f"candidate {candidate.candidate_id} references evidence outside source_event_ids: "
                    f"{sorted(undeclared)[:5]}"
                )
            for binding in candidate.bindings:
                if not binding.evidence:
                    raise ExtractionValidationError(
                        f"binding {binding.role.value} in {candidate.candidate_id} has no evidence"
                    )
                unknown_binding = set(binding.evidence) - candidate_events
                if unknown_binding:
                    raise ExtractionValidationError(
                        f"binding in {candidate.candidate_id} references evidence outside candidate: "
                        f"{sorted(unknown_binding)[:5]}"
                    )
                if not (binding.path or binding.symbol or binding.qualified_symbol):
                    raise ExtractionValidationError(
                        f"binding {binding.role.value} in {candidate.candidate_id} has no target"
                    )

    def _candidate_count(self, run_id: str) -> int:
        with self.repository.db.connection() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM memory_candidates WHERE run_id=?", (run_id,)).fetchone()[0]
            )


class FixedProvider:
    """Small test/helper provider for deterministic raw batch fixtures."""

    provider_name = "fixed"
    model_name = "fixture"
    prompt_version = "fixed-v1"

    def __init__(self, factory):
        self.factory = factory

    def extract(self, context):
        value = self.factory(context)
        return value


def batch_digest(batch: ExtractionBatch) -> str:
    """Expose a stable digest for diagnostics and tests."""

    import json

    payload = json.dumps(batch.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
