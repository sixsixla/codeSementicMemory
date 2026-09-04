"""Shared ingress service and deterministic JSONL replay."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from ..adapters import FixtureAdapter
from ..domain.events import EventEnvelope
from ..quality.service import QualityService
from ..storage.repository import ConflictError, IngestResult, MemoryRepository
from .redaction import redact_event


logger = logging.getLogger(__name__)


class IngestService:
    """Validate, redact, and persist events through one canonical path."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        quality_service: QualityService | None = None,
    ):
        self.repository = repository
        # Quality is a post-commit projection: a malformed classifier can
        # never roll back canonical facts, while every normal ingress path
        # still receives an evaluation before the caller continues.
        self.quality_service = quality_service or QualityService(repository)

    def ingest(self, event: EventEnvelope | dict[str, Any]) -> IngestResult:
        normalized = (
            event if isinstance(event, EventEnvelope) else EventEnvelope.model_validate(event)
        )
        result = self.repository.ingest(redact_event(normalized))
        if result.status in {"accepted", "duplicate"}:
            try:
                self.quality_service.evaluate_event_id(result.event_id)
            except Exception:
                # Quality is an additive projection.  A classifier/database
                # hiccup must not turn an already-committed canonical event
                # into an apparent ingest failure; the next replay repairs it.
                logger.exception("quality evaluation failed for event %s", result.event_id)
        return result

    def ingest_many(
        self, events: Iterable[EventEnvelope | dict[str, Any]], *, atomic: bool = True
    ) -> list[IngestResult]:
        normalized = [
            redact_event(
                event if isinstance(event, EventEnvelope) else EventEnvelope.model_validate(event)
            )
            for event in events
        ]
        results = self.repository.ingest_many(normalized, atomic=atomic)
        if results:
            # Evaluate the normalized envelopes directly in one projection
            # batch.  Looking every id back up and opening a transaction per
            # event makes a 10k-event replay needlessly expensive.
            by_id = {event.event_id: event for event in normalized}
            events = [
                event.canonical_dict()
                for result in results
                if result.status in {"accepted", "duplicate"}
                for event in [by_id.get(result.event_id)]
                if event is not None
            ]
            try:
                self.quality_service.evaluate_events(events)
            except Exception:
                logger.exception("quality evaluation failed for ingest batch (%d events)", len(events))
        return results


@dataclass
class ReplaySummary:
    path: str
    total_lines: int = 0
    accepted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    invalid: int = 0
    errors: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def replay_jsonl(
    path: str | Path,
    service: IngestService,
    *,
    strict: bool = False,
    max_errors: int = 100,
    batch_size: int = 500,
) -> ReplaySummary:
    """Replay one event envelope per line using the same service as HTTP.

    Valid records are committed in bounded SQLite transactions. A conflicting
    batch falls back to per-record writes so the report can identify the exact
    bad identity without losing unrelated records.
    """

    file_path = Path(path)
    summary = ReplaySummary(path=str(file_path.resolve()))
    adapter = FixtureAdapter()
    pending: list[EventEnvelope] = []
    pending_lines: list[int] = []

    def account(result: IngestResult, line_number: int) -> None:
        if result.status == "accepted":
            summary.accepted += 1
        else:
            summary.duplicates += 1

    def record_conflict(exc: ConflictError, line_number: int) -> None:
        summary.conflicts += 1
        summary.errors.append(
            {
                "line": line_number,
                "kind": "conflict",
                "message": str(exc),
                "event_id": exc.existing_event_id,
            }
        )

    def flush() -> None:
        if not pending:
            return
        records = list(pending)
        lines = list(pending_lines)
        pending.clear()
        pending_lines.clear()
        try:
            results = service.ingest_many(records)
            for result, line_number in zip(results, lines, strict=True):
                account(result, line_number)
        except ConflictError:
            # Batch transactions intentionally roll back on a conflict. Retry
            # independently to preserve useful per-line diagnostics.
            for event, line_number in zip(records, lines, strict=True):
                try:
                    account(service.ingest(event), line_number)
                except ConflictError as exc:
                    record_conflict(exc, line_number)
                    if strict:
                        raise

    with file_path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            summary.total_lines += 1
            try:
                data = json.loads(raw_line)
                if not isinstance(data, dict):
                    raise ValueError("JSONL record must be an object")
                pending.append(adapter.normalize(data))
                pending_lines.append(line_number)
                if len(pending) >= max(1, batch_size):
                    flush()
            except ConflictError as exc:
                record_conflict(exc, line_number)
                if strict:
                    raise
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                summary.invalid += 1
                summary.errors.append({"line": line_number, "kind": "invalid", "message": str(exc)})
                if strict:
                    raise
            if len(summary.errors) >= max_errors:
                flush()
                summary.errors.append(
                    {"line": line_number, "kind": "limit", "message": "error limit reached"}
                )
                break
    flush()
    return summary
