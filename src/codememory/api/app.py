"""FastAPI application exposing the local ingest and inspection surface."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from ..config import default_db_path
from ..consolidation.service import ConsolidationService
from ..consolidation.store import CardStore
from ..domain.events import EventEnvelope
from ..extraction.service import ExtractionService
from ..extraction.store import ExtractionStore
from ..extraction.providers import provider_from_name
from ..ingest.service import IngestService
from ..storage.database import Database
from ..storage.repository import ConflictError, MemoryRepository


class BatchRequest(BaseModel):
    # Keep raw objects here so one malformed record does not discard a whole
    # batch; each record is validated independently below.
    events: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class ReplayRequest(BaseModel):
    events: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class OutboxClaimRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    lease_seconds: int = Field(default=60, ge=1, le=86_400)


class OutboxFailRequest(BaseModel):
    error: str = Field(min_length=1, max_length=2000)
    retry_at: str | None = None
    retry_delay_seconds: int | None = Field(default=None, ge=0, le=86_400)
    dead: bool = False


class ExtractRequest(BaseModel):
    provider: str = Field(default="mock", min_length=1, max_length=100)
    session_id: str | None = Field(default=None, max_length=300)
    force: bool = False


class ConsolidateRequest(BaseModel):
    limit: int = Field(default=1000, ge=1, le=100_000)
    candidate_ids: list[str] | None = Field(default=None, max_length=1000)


class CardTransitionRequest(BaseModel):
    status: str = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1, max_length=2000)
    actor: str = Field(default="api", min_length=1, max_length=200)


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Create an app instance; initialization is deterministic and testable."""

    database = Database(db_path or default_db_path())
    repository = MemoryRepository(database)
    service = IngestService(repository)
    extraction_store = ExtractionStore(database)
    extraction_service = ExtractionService(repository, store=extraction_store)
    card_store = CardStore(database)
    consolidation_service = ConsolidationService(repository, store=card_store)
    app = FastAPI(
        title="CodeSementicMemory",
        version="0.1.0",
        description="SQLite-first coding event memory service",
    )
    app.state.database = database
    app.state.repository = repository
    app.state.ingest_service = service
    app.state.extraction_store = extraction_store
    app.state.extraction_service = extraction_service
    app.state.card_store = card_store
    app.state.consolidation_service = consolidation_service

    web_candidates = (
        Path(__file__).resolve().parents[3] / "web",
        Path(sys.prefix) / "web",
        Path(sys.base_prefix) / "web",
        Path.cwd() / "web",
    )
    web_dir = next((candidate for candidate in web_candidates if candidate.exists()), None)
    if web_dir is not None:
        app.mount("/web", StaticFiles(directory=str(web_dir)), name="web")

        @app.get("/", include_in_schema=False)
        async def web_index() -> FileResponse:
            return FileResponse(web_dir / "index.html")

    @app.exception_handler(ConflictError)
    async def conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "event_conflict",
                "message": str(exc),
                "existing_event_id": exc.existing_event_id,
            },
        )

    @app.get("/v1/health")
    async def health(
        deep: bool = Query(default=False, description="run the exhaustive SQLite integrity check"),
    ) -> dict[str, Any]:
        # A local history database can be hundreds of megabytes.  Integrity
        # scans belong to an explicit deep probe, not every UI heartbeat.
        return repository.health(check_integrity=deep)

    @app.post("/v1/events")
    async def ingest_event(event: EventEnvelope) -> JSONResponse:
        result = service.ingest(event)
        code = status.HTTP_201_CREATED if result.status == "accepted" else status.HTTP_200_OK
        return JSONResponse(status_code=code, content=result.as_dict())

    @app.post("/v1/events/batch")
    async def ingest_batch(request: BatchRequest) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for raw_event in request.events:
            try:
                event = EventEnvelope.model_validate(raw_event)
                results.append(service.ingest(event).as_dict())
            except ConflictError as exc:
                results.append(
                    {
                        "status": "conflict",
                        "event_id": raw_event.get("event_id"),
                        "accepted_seq": raw_event.get("seq"),
                        "outbox_job_id": None,
                        "error": str(exc),
                        "existing_event_id": exc.existing_event_id,
                    }
                )
            except ValidationError as exc:
                results.append(
                    {
                        "status": "invalid",
                        "event_id": raw_event.get("event_id"),
                        "accepted_seq": raw_event.get("seq"),
                        "outbox_job_id": None,
                        "error": str(exc),
                    }
                )
        return {
            "total": len(results),
            "accepted": sum(item["status"] == "accepted" for item in results),
            "duplicates": sum(item["status"] == "duplicate" for item in results),
            "conflicts": sum(item["status"] == "conflict" for item in results),
            "invalid": sum(item["status"] == "invalid" for item in results),
            "results": results,
        }

    @app.post("/v1/replay")
    async def replay_events(request: ReplayRequest) -> dict[str, Any]:
        results = []
        for raw_event in request.events:
            try:
                event = EventEnvelope.model_validate(raw_event)
                results.append(service.ingest(event).as_dict())
            except ConflictError as exc:
                results.append(
                    {
                        "status": "conflict",
                        "event_id": raw_event.get("event_id"),
                        "accepted_seq": raw_event.get("seq"),
                        "outbox_job_id": None,
                        "error": str(exc),
                        "existing_event_id": exc.existing_event_id,
                    }
                )
            except ValidationError as exc:
                results.append(
                    {
                        "status": "invalid",
                        "event_id": raw_event.get("event_id"),
                        "accepted_seq": raw_event.get("seq"),
                        "outbox_job_id": None,
                        "error": str(exc),
                    }
                )
        return {"total": len(results), "results": results}

    @app.get("/v1/tasks/{task_id}/timeline")
    async def task_timeline(
        task_id: str,
        session_id: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=5000),
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "session_id": session_id,
            "events": repository.timeline(task_id, session_id=session_id, limit=limit),
        }

    @app.get("/v1/tasks")
    async def tasks(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
        with database.connection() as conn:
            rows = conn.execute(
                "SELECT task_id,project_id,title,status,created_at,updated_at FROM tasks "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "tasks": [
                {
                    "task_id": str(row["task_id"]),
                    "project_id": str(row["project_id"]),
                    "title": row["title"],
                    "status": str(row["status"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
                for row in rows
            ]
        }

    @app.get("/v1/tasks/{task_id}/memories")
    async def task_memories(
        task_id: str,
        kind: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "memories": extraction_store.list_candidates(task_id=task_id, kind=kind, limit=limit),
        }

    @app.get("/v1/tasks/{task_id}/extraction-runs")
    async def task_extraction_runs(
        task_id: str, limit: int = Query(default=100, ge=1, le=1000)
    ) -> dict[str, Any]:
        return {"task_id": task_id, "runs": extraction_store.list_runs(task_id=task_id, limit=limit)}

    @app.post("/v1/tasks/{task_id}/extract")
    async def extract_task(task_id: str, request: ExtractRequest | None = None) -> dict[str, Any]:
        request = request or ExtractRequest()
        try:
            provider = provider_from_name(request.provider)
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "unsupported_provider", "provider": request.provider, "message": str(exc)},
            )
        task_extraction = ExtractionService(
            repository,
            store=extraction_store,
            provider=provider,
        )
        result = task_extraction.extract_task(
            task_id, session_id=request.session_id, force=request.force
        )
        return result.as_dict()

    @app.post("/v1/tasks/{task_id}/consolidate")
    async def consolidate_task(task_id: str, request: ConsolidateRequest | None = None) -> dict[str, Any]:
        request = request or ConsolidateRequest()
        result = consolidation_service.consolidate(
            task_id=task_id,
            candidate_ids=request.candidate_ids,
            limit=request.limit,
        )
        return {"task_id": task_id, **result.as_dict()}

    @app.get("/v1/cards/search")
    async def search_cards(
        q: str = Query(min_length=1, max_length=500),
        project_id: str | None = Query(default=None),
        task_id: str | None = Query(default=None),
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        return {
            "query": q,
            "results": card_store.search(
                q,
                project_id=project_id,
                task_id=task_id,
                status=status_filter,
                limit=limit,
            ),
        }

    @app.get("/v1/cards")
    async def cards(
        project_id: str | None = Query(default=None),
        task_id: str | None = Query(default=None),
        status_filter: str | None = Query(default=None, alias="status"),
        kind: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10_000),
    ) -> dict[str, Any]:
        return {
            "cards": card_store.list_cards(
                project_id=project_id,
                task_id=task_id,
                status=status_filter,
                kind=kind,
                limit=limit,
            )
        }

    @app.post("/v1/cards/{card_id}/transition")
    async def transition_card(card_id: str, request: CardTransitionRequest) -> JSONResponse:
        try:
            result = consolidation_service.transition_card(
                card_id,
                request.status,
                reason=request.reason,
                actor=request.actor,
            )
        except ValueError as exc:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(exc)})
        if not result.get("updated") and result.get("reason") == "not_found":
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=result)
        return JSONResponse(content=result)

    @app.get("/v1/cards/{card_id}/history")
    async def card_history(card_id: str) -> JSONResponse:
        card = card_store.get_card(card_id)
        if card is None:
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": "card_not_found", "card_id": card_id})
        return JSONResponse(content={"card_id": card_id, "versions": card["versions"]})

    @app.get("/v1/cards/{card_id}/relations")
    async def card_relations(card_id: str) -> JSONResponse:
        card = card_store.get_card(card_id)
        if card is None:
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"error": "card_not_found", "card_id": card_id})
        return JSONResponse(content={"card_id": card_id, "relations": card["links"]})

    @app.get("/v1/memories/search")
    async def search_memories(
        q: str = Query(min_length=1, max_length=500),
        task_id: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        return {
            "query": q,
            "task_id": task_id,
            "results": extraction_store.search(q, task_id=task_id, limit=limit),
        }

    # Keep the static ``search`` path above the dynamic candidate path. Starlette
    # resolves routes in declaration order, so otherwise ``/search`` would be
    # interpreted as a candidate id.
    @app.get("/v1/memories/{candidate_id}")
    async def memory_detail(candidate_id: str) -> JSONResponse:
        candidate = extraction_store.get_candidate(candidate_id)
        if candidate is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "memory_not_found", "candidate_id": candidate_id},
            )
        return JSONResponse(content=candidate)

    @app.get("/v1/cards/{card_id}")
    async def card_detail(card_id: str) -> JSONResponse:
        card = card_store.get_card(card_id)
        if card is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "card_not_found", "card_id": card_id},
            )
        return JSONResponse(content=card)

    @app.get("/v1/graph")
    async def graph(
        task_id: str | None = Query(default=None),
        limit: int = Query(default=300, ge=20, le=2000),
    ) -> dict[str, Any]:
        graph_payload = extraction_store.graph(task_id=task_id, limit=limit)
        return card_store.extend_graph(graph_payload, task_id=task_id, limit=limit)

    @app.get("/v1/search")
    async def search(
        q: str = Query(min_length=1, max_length=500),
        project_id: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"query": q, "results": repository.search(q, project_id=project_id, limit=limit)}

    @app.get("/v1/outbox")
    async def outbox(
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        jobs = repository.list_outbox(status=status_filter, limit=limit)
        return {"counts": repository.outbox_count(), "jobs": [job.as_dict() for job in jobs]}

    @app.post("/v1/outbox/claim")
    async def claim_outbox(request: OutboxClaimRequest) -> dict[str, Any]:
        jobs = repository.claim_outbox(limit=request.limit, lease_seconds=request.lease_seconds)
        return {"jobs": [job.as_dict() for job in jobs], "counts": repository.outbox_count()}

    @app.post("/v1/outbox/{job_id}/complete")
    async def complete_outbox(job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "completed": repository.complete_outbox(job_id)}

    @app.post("/v1/outbox/{job_id}/fail")
    async def fail_outbox(job_id: str, request: OutboxFailRequest) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "updated": repository.fail_outbox(
                job_id,
                request.error,
                retry_at=request.retry_at,
                retry_delay_seconds=request.retry_delay_seconds,
                dead=request.dead,
            ),
        }

    return app
