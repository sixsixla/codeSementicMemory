"""FastAPI application exposing the local ingest and inspection surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from ..config import default_db_path
from ..domain.events import EventEnvelope
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


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Create an app instance; initialization is deterministic and testable."""

    database = Database(db_path or default_db_path())
    repository = MemoryRepository(database)
    service = IngestService(repository)
    app = FastAPI(
        title="CodeSementicMemory",
        version="0.1.0",
        description="SQLite-first coding event memory service",
    )
    app.state.database = database
    app.state.repository = repository
    app.state.ingest_service = service

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
    async def health() -> dict[str, Any]:
        return repository.health()

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
