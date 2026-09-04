"""A small synchronous outbox worker for Phase 2A.

The worker is intentionally invoked by a process/scheduler or the CLI.  Event
ingest only enqueues jobs and never calls this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..extraction.service import ExtractionService
from ..storage.repository import MemoryRepository


@dataclass(frozen=True)
class WorkerSummary:
    claimed: int
    completed: int
    failed: int
    results: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExtractionWorker:
    def __init__(self, repository: MemoryRepository, service: ExtractionService | None = None):
        self.repository = repository
        self.service = service or ExtractionService(repository)

    def run_once(self, *, limit: int = 20, lease_seconds: int = 120) -> WorkerSummary:
        jobs = self.repository.claim_outbox(
            limit=limit,
            lease_seconds=lease_seconds,
            job_type="event.ingested",
        )
        results: list[dict[str, Any]] = []
        completed = 0
        failed = 0
        for job in jobs:
            if job.job_type != "event.ingested":
                self.repository.complete_outbox(job.job_id)
                completed += 1
                results.append({"job_id": job.job_id, "status": "skipped", "job_type": job.job_type})
                continue
            task_id = str(job.payload.get("task_id") or "")
            event_id = str(job.payload.get("event_id") or job.aggregate_id)
            if not task_id:
                event = self.repository.get_event(event_id)
                task_id = str(event.get("task_id") or "") if event else ""
            if not task_id:
                self.repository.fail_outbox(job.job_id, "event.ingested job has no task_id", dead=True)
                failed += 1
                results.append({"job_id": job.job_id, "status": "failed", "error": "missing task_id"})
                continue
            try:
                extraction = self.service.extract_task(task_id)
                if extraction.status in {"failed", "dead"}:
                    self.repository.fail_outbox(
                        job.job_id,
                        extraction.error or "extraction dead-lettered",
                        dead=extraction.status == "dead",
                    )
                    failed += 1
                else:
                    self.repository.complete_outbox(job.job_id)
                    completed += 1
                results.append({"job_id": job.job_id, **extraction.as_dict()})
            except Exception as exc:  # pragma: no cover - defensive worker boundary
                self.repository.fail_outbox(job.job_id, f"{type(exc).__name__}: {exc}")
                failed += 1
                results.append({"job_id": job.job_id, "status": "failed", "error": str(exc)})
        return WorkerSummary(len(jobs), completed, failed, tuple(results))
