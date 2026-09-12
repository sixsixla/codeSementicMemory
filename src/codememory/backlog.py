"""Task-coalesced processing for the durable event.ingested backlog."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .consolidation.service import ConsolidationService
from .consolidation.store import CardStore
from .extraction.providers import provider_from_name
from .extraction.service import ExtractionService
from .extraction.store import ExtractionStore
from .storage.repository import MemoryRepository
from .storage.scope import raw_project_ids_for_logical


CODING_EVENT_TYPES = (
    "file_read",
    "file_edit",
    "tool_call",
    "tool_result",
    "command_run",
    "validation_run",
    "vcs_change",
)


@dataclass(frozen=True)
class BacklogTask:
    task_id: str
    project_id: str
    title: str | None
    pending_jobs: int
    event_count: int
    coding_evidence_count: int
    successful_extraction: bool
    latest_event_at: str | None
    oldest_pending_at: str | None
    source_adapters: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_adapters"] = list(self.source_adapters)
        value["eligible"] = self.coding_evidence_count > 0
        return value


@dataclass(frozen=True)
class BacklogProcessResult:
    planned_tasks: int
    processed_tasks: int
    extracted: int
    duplicates: int
    failed: int
    skipped: int
    empty: int
    candidates: int
    cards_created: int
    cards_merged: int
    cards_new_versions: int
    outbox_completed: int
    tasks: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["tasks"] = list(self.tasks)
        return value


class BacklogService:
    """Plan and process pending event jobs once per unique task."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository
        self.database = repository.db

    def plan(
        self,
        *,
        project_id: str | None = None,
        logical_project_id: str | None = None,
        limit_tasks: int = 100,
        min_events: int = 2,
        include_extracted: bool = False,
    ) -> list[BacklogTask]:
        """Return a deterministic, read-only task backlog plan."""

        raw_ids = raw_project_ids_for_logical(self.database, logical_project_id)
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("t.project_id=?")
            params.append(project_id)
        if logical_project_id:
            if raw_ids:
                placeholders = ",".join("?" for _ in raw_ids)
                clauses.append(f"t.project_id IN ({placeholders})")
                params.extend(raw_ids)
            else:
                return []
        if not include_extracted:
            clauses.append("x.task_id IS NULL")
        where = " AND ".join(clauses)
        where_prefix = f"{where} AND " if where else ""
        coding_placeholders = ",".join("?" for _ in CODING_EVENT_TYPES)
        query_params = [*CODING_EVENT_TYPES, *params]
        query_params.append(max(0, int(min_events)))
        query_params.append(max(1, min(int(limit_tasks), 10_000)))
        with self.database.connection() as conn:
            rows = conn.execute(
                "WITH pending AS ("
                "SELECT json_extract(payload_json,'$.task_id') AS task_id,"
                "COUNT(*) AS pending_jobs,MIN(created_at) AS oldest_pending_at "
                "FROM outbox WHERE job_type='event.ingested' "
                "AND status IN ('pending','retry') "
                "GROUP BY json_extract(payload_json,'$.task_id')),"
                "event_stats AS ("
                "SELECT e.task_id,COUNT(*) AS event_count,"
                f"SUM(CASE WHEN e.event_type IN ({coding_placeholders}) "
                "OR (q.role IN ('code_evidence','outcome') "
                "AND q.decision IN ('accepted','review')) THEN 1 ELSE 0 END) "
                "AS coding_evidence_count,MAX(e.occurred_at) AS latest_event_at,"
                "GROUP_CONCAT(DISTINCT e.producer_adapter) AS source_adapters "
                "FROM events e LEFT JOIN event_quality_evaluations q ON q.event_id=e.event_id "
                "GROUP BY e.task_id),"
                "successful AS (SELECT task_id FROM extraction_runs WHERE status='succeeded' "
                "GROUP BY task_id) "
                "SELECT t.task_id,t.project_id,t.title,p.pending_jobs,s.event_count,"
                "s.coding_evidence_count,s.latest_event_at,p.oldest_pending_at,s.source_adapters,"
                "CASE WHEN x.task_id IS NULL THEN 0 ELSE 1 END AS successful_extraction "
                "FROM tasks t JOIN pending p ON p.task_id=t.task_id "
                "JOIN event_stats s ON s.task_id=t.task_id "
                "LEFT JOIN successful x ON x.task_id=t.task_id "
                f"WHERE {where_prefix}s.event_count>=? "
                "ORDER BY coding_evidence_count DESC,latest_event_at DESC,t.task_id LIMIT ?",
                tuple(query_params),
            ).fetchall()
        return [
            BacklogTask(
                task_id=str(row["task_id"]),
                project_id=str(row["project_id"]),
                title=row["title"],
                pending_jobs=int(row["pending_jobs"]),
                event_count=int(row["event_count"]),
                coding_evidence_count=int(row["coding_evidence_count"] or 0),
                successful_extraction=bool(row["successful_extraction"]),
                latest_event_at=row["latest_event_at"],
                oldest_pending_at=row["oldest_pending_at"],
                source_adapters=tuple(
                    sorted(item for item in str(row["source_adapters"] or "").split(",") if item)
                ),
            )
            for row in rows
        ]

    def process(
        self,
        *,
        project_id: str | None = None,
        logical_project_id: str | None = None,
        limit_tasks: int = 25,
        min_events: int = 2,
        provider: str = "mock",
        consolidate: bool = True,
        complete_outbox: bool = True,
        dry_run: bool = False,
        include_non_coding: bool = False,
    ) -> BacklogProcessResult | dict[str, Any]:
        planned = self.plan(
            project_id=project_id,
            logical_project_id=logical_project_id,
            limit_tasks=limit_tasks,
            min_events=min_events,
            include_extracted=True,
        )
        if dry_run:
            return {
                "dry_run": True,
                "planned_tasks": len(planned),
                "tasks": [item.as_dict() for item in planned],
            }

        extraction = ExtractionService(
            self.repository,
            store=ExtractionStore(self.database),
            provider=provider_from_name(provider),
        )
        cards = ConsolidationService(
            self.repository,
            store=CardStore(self.database),
        )
        stats = {
            "processed_tasks": 0,
            "extracted": 0,
            "duplicates": 0,
            "failed": 0,
            "skipped": 0,
            "empty": 0,
            "candidates": 0,
            "cards_created": 0,
            "cards_merged": 0,
            "cards_new_versions": 0,
            "outbox_completed": 0,
        }
        task_results: list[dict[str, Any]] = []
        for item in planned:
            if item.coding_evidence_count == 0 and not include_non_coding:
                stats["skipped"] += 1
                task_results.append(
                    {
                        "task_id": item.task_id,
                        "project_id": item.project_id,
                        "status": "skipped",
                        "reason": "no_coding_evidence",
                    }
                )
                continue
            lease = self.repository.claim_outbox_for_task(item.task_id)
            if lease is None:
                stats["skipped"] += 1
                task_results.append(
                    {"task_id": item.task_id, "status": "skipped", "reason": "lease_unavailable"}
                )
                continue
            stats["processed_tasks"] += 1
            try:
                extracted = extraction.extract_task(item.task_id).as_dict()
                task_result: dict[str, Any] = {
                    "task_id": item.task_id,
                    "project_id": item.project_id,
                    "extraction": extracted,
                }
                stats["candidates"] += int(extracted.get("candidate_count") or 0)
                if extracted["status"] == "extracted":
                    stats["extracted"] += 1
                    if int(extracted.get("candidate_count") or 0) == 0:
                        stats["empty"] += 1
                    if consolidate:
                        consolidated = cards.consolidate(task_id=item.task_id).as_dict()
                        task_result["consolidation"] = consolidated
                        for key in (
                            "created",
                            "merged",
                            "new_versions",
                        ):
                            target = {
                                "created": "cards_created",
                                "merged": "cards_merged",
                                "new_versions": "cards_new_versions",
                            }[key]
                            stats[target] += int(consolidated.get(key) or 0)
                elif extracted["status"] == "duplicate":
                    stats["duplicates"] += 1
                elif extracted["status"] == "in_progress":
                    stats["skipped"] += 1
                    self.repository.release_outbox(lease.job_id)
                    task_result["status"] = "in_progress"
                else:
                    stats["failed"] += 1
                if extracted["status"] in {"extracted", "duplicate"}:
                    if complete_outbox:
                        stats["outbox_completed"] += int(
                            self.repository.complete_outbox(lease.job_id)
                        )
                        stats["outbox_completed"] += self.repository.complete_outbox_for_task(
                            item.task_id
                        )
                    else:
                        self.repository.release_outbox(lease.job_id)
                elif extracted["status"] == "in_progress":
                    pass
                else:
                    error = extracted.get("error") or "task extraction failed"
                    self.repository.fail_outbox(
                        lease.job_id, str(error), dead=extracted["status"] == "dead"
                    )
                    self.repository.fail_outbox_for_task(
                        item.task_id,
                        str(error),
                        dead=extracted["status"] == "dead",
                    )
                task_results.append(task_result)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                stats["failed"] += 1
                self.repository.fail_outbox(lease.job_id, error)
                self.repository.fail_outbox_for_task(item.task_id, error)
                task_results.append({"task_id": item.task_id, "status": "failed", "error": error})
        return BacklogProcessResult(
            planned_tasks=len(planned),
            tasks=tuple(task_results),
            **{key: value for key, value in stats.items()},
        )
