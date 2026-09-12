"""Repository operations for events, entities, search, and outbox jobs."""

from __future__ import annotations

import hashlib
import json
import base64
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..domain.events import EventEnvelope, EventType
from .database import Database
from .scope import raw_project_ids_for_logical


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_now() -> str:
    return utc_now().isoformat()


def json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def json_value(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class ConflictError(RuntimeError):
    """Raised when an idempotency key is reused for different content."""

    def __init__(self, message: str, *, existing_event_id: str | None = None):
        super().__init__(message)
        self.existing_event_id = existing_event_id


@dataclass(frozen=True)
class IngestResult:
    status: str
    event_id: str
    accepted_seq: int
    outbox_job_id: str | None
    duplicate_of: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutboxJob:
    job_id: str
    job_type: str
    aggregate_id: str
    dedupe_key: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    available_at: str
    lease_until: str | None
    last_error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OutboxJob":
        return cls(
            job_id=str(row["job_id"]),
            job_type=str(row["job_type"]),
            aggregate_id=str(row["aggregate_id"]),
            dedupe_key=str(row["dedupe_key"]),
            payload=json_value(row["payload_json"], {}),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            available_at=str(row["available_at"]),
            lease_until=row["lease_until"],
            last_error=row["last_error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryRepository:
    """Canonical persistence API used by HTTP, replay, and future workers."""

    def __init__(self, database: Database, *, artifact_max_bytes: int | None = None):
        self.db = database
        configured_limit = os.environ.get("CODEMEMORY_ARTIFACT_MAX_BYTES")
        self.artifact_max_bytes = max(
            1,
            int(artifact_max_bytes or configured_limit or 1_000_000),
        )
        self.db.ensure_initialized()

    @staticmethod
    def _event_hash(event: EventEnvelope) -> str:
        canonical = json_text(event.canonical_dict()).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _repo_id(event: EventEnvelope) -> str | None:
        context = event.context
        if context.repo_id:
            return context.repo_id
        if context.root_path:
            digest = hashlib.sha256(
                f"{event.project_id}\n{context.root_path}".encode("utf-8")
            ).hexdigest()[:24]
            return f"repo-{digest}"
        return None

    @staticmethod
    def _task_title(event: EventEnvelope) -> str | None:
        for key in ("title", "summary", "text", "message"):
            value = event.payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return None

    @staticmethod
    def _event_search_text(event: EventEnvelope) -> str:
        pieces: list[str] = [event.event_type.value, event.project_id, event.task_id]
        if event.session_id:
            pieces.append(event.session_id)
        pieces.extend(
            json_text(value)
            for value in (
                event.payload,
                event.context.model_dump(mode="json"),
                [
                    artifact.model_dump(mode="json")
                    if hasattr(artifact, "model_dump")
                    else dict(artifact)
                    for artifact in event.artifacts
                ],
            )
        )
        return " ".join(pieces)

    def _artifact_bytes(self, data: Mapping[str, Any]) -> bytes | None:
        content = data.get("content")
        if content is None:
            return None
        if data.get("content_encoding") == "base64":
            try:
                return base64.b64decode(str(content), validate=True)
            except (ValueError, TypeError):
                return str(content).encode("utf-8")
        return str(content).encode("utf-8")

    def _artifact_identity(self, event: EventEnvelope, artifact: Any) -> tuple[str, str]:
        data = (
            artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact)
        )
        supplied = data.get("sha256")
        if supplied:
            digest = str(supplied)
        elif (content_bytes := self._artifact_bytes(data)) is not None:
            digest = hashlib.sha256(content_bytes).hexdigest()
        else:
            # A reference without content bytes still gets a stable identity;
            # the metadata records that it is not a content hash yet.
            digest = hashlib.sha256(
                json_text(
                    {
                        "project_id": event.project_id,
                        "repo_id": event.context.repo_id,
                        "path": data.get("path"),
                        "uri": data.get("uri"),
                        "kind": data.get("kind"),
                    }
                ).encode("utf-8")
            ).hexdigest()
        return digest, f"artifact-{digest[:32]}"

    def _prepare_artifact(self, event: EventEnvelope, artifact: Any) -> dict[str, Any]:
        data = (
            artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact)
        )
        digest, artifact_id = self._artifact_identity(event, artifact)
        metadata = dict(data.get("metadata") or {})
        content_bytes = self._artifact_bytes(data)
        supplied_size = data.get("size_bytes")
        original_size = len(content_bytes) if content_bytes is not None else supplied_size
        truncated = bool(data.get("truncated", False))
        excerpt: str | None = None
        if content_bytes is not None:
            if len(content_bytes) > self.artifact_max_bytes:
                excerpt = content_bytes[: self.artifact_max_bytes].decode("utf-8", errors="replace")
                truncated = True
                metadata["original_size_bytes"] = len(content_bytes)
            else:
                excerpt = content_bytes.decode("utf-8", errors="replace")
        if not data.get("sha256") and content_bytes is None:
            metadata.setdefault("identity_only", True)
        if truncated:
            metadata["truncated"] = True
        return {
            "artifact_id": artifact_id,
            "sha256": digest,
            "kind": data["kind"],
            "uri": data.get("uri"),
            "path": data.get("path"),
            "mime_type": data.get("mime_type"),
            "size_bytes": original_size,
            "content_excerpt": excerpt,
            "truncated": int(truncated),
            "metadata": metadata,
        }

    def _ensure_artifacts(
        self, conn: sqlite3.Connection, event: EventEnvelope, now: str
    ) -> tuple[list[tuple[str, int]], list[dict[str, Any]]]:
        links: list[tuple[str, int]] = []
        stored_refs: list[dict[str, Any]] = []
        for ordinal, artifact in enumerate(event.artifacts):
            prepared = self._prepare_artifact(event, artifact)
            stored_ref = (
                artifact.model_dump(mode="json")
                if hasattr(artifact, "model_dump")
                else dict(artifact)
            )
            if stored_ref.get("content") is not None:
                # Keep the event row a bounded reference. The artifact table
                # owns the excerpt; raw content is never duplicated into the
                # append-only event JSON.
                stored_ref["content"] = None
                stored_ref.setdefault("metadata", {})["content_sha256"] = prepared["sha256"]
                stored_ref["metadata"]["content_stored"] = True
                stored_ref["truncated"] = bool(prepared["truncated"])
            stored_refs.append(stored_ref)
            conn.execute(
                "INSERT INTO artifacts(artifact_id, sha256, kind, uri, path, mime_type, size_bytes, content_excerpt, truncated, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sha256) DO UPDATE SET kind=excluded.kind, uri=COALESCE(excluded.uri, artifacts.uri), "
                "path=COALESCE(excluded.path, artifacts.path), mime_type=COALESCE(excluded.mime_type, artifacts.mime_type), "
                "size_bytes=COALESCE(excluded.size_bytes, artifacts.size_bytes), content_excerpt=COALESCE(excluded.content_excerpt, artifacts.content_excerpt), "
                "truncated=MAX(artifacts.truncated, excluded.truncated), metadata_json=excluded.metadata_json",
                (
                    prepared["artifact_id"],
                    prepared["sha256"],
                    prepared["kind"],
                    prepared["uri"],
                    prepared["path"],
                    prepared["mime_type"],
                    prepared["size_bytes"],
                    prepared["content_excerpt"],
                    prepared["truncated"],
                    json_text(prepared["metadata"]),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT artifact_id FROM artifacts WHERE sha256=?", (prepared["sha256"],)
            ).fetchone()
            links.append((str(row[0]), ordinal))
        return links, stored_refs

    def _ensure_entities(self, conn: sqlite3.Connection, event: EventEnvelope, now: str) -> None:
        # Entity timestamps describe the source activity, not the time a
        # large replay happened.  Keeping the event time here makes task
        # listing and recent-history selection useful after importing an old
        # Codex archive, while ``ingested_at`` on the event still records when
        # this process accepted it.
        source_time = event.occurred_at.isoformat()
        existing_task = conn.execute(
            "SELECT project_id FROM tasks WHERE task_id=?", (event.task_id,)
        ).fetchone()
        if existing_task and str(existing_task["project_id"]) != event.project_id:
            raise ConflictError(
                "task_id is already owned by a different project", existing_event_id=event.task_id
            )
        conn.execute(
            "INSERT INTO projects(project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET updated_at=MAX(projects.updated_at, excluded.updated_at)",
            (event.project_id, event.project_id, source_time, source_time),
        )
        title = self._task_title(event)
        conn.execute(
            "INSERT INTO tasks(task_id, project_id, title, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET project_id=excluded.project_id, "
            "title=COALESCE(tasks.title, excluded.title), updated_at=MAX(tasks.updated_at, excluded.updated_at)",
            (event.task_id, event.project_id, title, source_time, source_time),
        )
        repo_id = self._repo_id(event)
        if repo_id:
            context = event.context
            existing_repo = conn.execute(
                "SELECT project_id, root_path FROM repositories WHERE repo_id=?", (repo_id,)
            ).fetchone()
            expected_root = context.root_path or context.cwd or "unknown"
            if existing_repo and (
                str(existing_repo["project_id"]) != event.project_id
                or str(existing_repo["root_path"]) != expected_root
            ):
                raise ConflictError(
                    "repo_id is already associated with a different project or root",
                    existing_event_id=repo_id,
                )
            conn.execute(
                "INSERT INTO repositories(repo_id, project_id, root_path, vcs_type, remote_url, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo_id) DO UPDATE SET project_id=excluded.project_id, root_path=excluded.root_path, "
                "vcs_type=excluded.vcs_type, updated_at=MAX(repositories.updated_at, excluded.updated_at)",
                (
                    repo_id,
                    event.project_id,
                    expected_root,
                    "p4" if context.p4_changelist else "git" if context.commit_id else "unknown",
                    None,
                    source_time,
                    source_time,
                ),
            )
        if event.session_id:
            # A session_started event is the usual first row, but accepting a
            # late or partial event should still create a usable session.
            if event.event_type == EventType.SESSION_STARTED:
                started_at = event.occurred_at.isoformat()
            else:
                started_at = event.occurred_at.isoformat()
            existing_session = conn.execute(
                "SELECT task_id, agent_id, adapter FROM sessions WHERE session_id=?",
                (event.session_id,),
            ).fetchone()
            if existing_session and (
                str(existing_session["task_id"]) != event.task_id
                or str(existing_session["agent_id"]) != event.producer.agent_id
                or str(existing_session["adapter"]) != event.producer.adapter
            ):
                raise ConflictError(
                    "session_id is already associated with a different task or producer",
                    existing_event_id=event.session_id,
                )
            conn.execute(
                "INSERT INTO sessions(session_id, task_id, agent_id, adapter, started_at, status, last_seq, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, '{}') "
                "ON CONFLICT(session_id) DO UPDATE SET task_id=excluded.task_id, agent_id=excluded.agent_id, "
                "adapter=excluded.adapter, last_seq=MAX(sessions.last_seq, excluded.last_seq)",
                (
                    event.session_id,
                    event.task_id,
                    event.producer.agent_id,
                    event.producer.adapter,
                    started_at,
                    event.seq,
                ),
            )

    def _existing_by_identity(
        self, conn: sqlite3.Connection, event: EventEnvelope
    ) -> sqlite3.Row | None:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
        if row:
            return row
        if event.external_event_id:
            row = conn.execute(
                "SELECT * FROM events WHERE producer_agent_id = ? AND producer_adapter = ? "
                "AND external_event_id = ?",
                (event.producer.agent_id, event.producer.adapter, event.external_event_id),
            ).fetchone()
            if row:
                return row
        if event.session_id:
            row = conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND seq = ?",
                (event.session_id, event.seq),
            ).fetchone()
            if row:
                return row
        return None

    @staticmethod
    def _same_hash(row: sqlite3.Row, event_hash: str) -> bool:
        return str(row["event_hash"]) == event_hash

    def _ingest_with_connection(
        self, conn: sqlite3.Connection, event: EventEnvelope, *, now: str | None = None
    ) -> IngestResult:
        """Insert one event while the caller owns the transaction."""

        event_hash = self._event_hash(event)
        now = now or iso_now()
        event_data = event.canonical_dict()
        existing = self._existing_by_identity(conn, event)
        if existing:
            if self._same_hash(existing, event_hash):
                job = conn.execute(
                    "SELECT job_id FROM outbox WHERE job_type='event.ingested' AND dedupe_key=?",
                    (str(existing["event_id"]),),
                ).fetchone()
                return IngestResult(
                    status="duplicate",
                    event_id=str(existing["event_id"]),
                    accepted_seq=int(existing["seq"]),
                    outbox_job_id=str(job[0]) if job else None,
                    duplicate_of=str(existing["event_id"]),
                )
            raise ConflictError(
                "idempotency identity already contains different event content",
                existing_event_id=str(existing["event_id"]),
            )

        self._ensure_entities(conn, event, now)
        artifact_links, stored_artifacts = self._ensure_artifacts(conn, event, now)
        event_data["artifacts"] = stored_artifacts
        try:
            conn.execute(
                "INSERT INTO events(event_id, external_event_id, schema_version, event_type, occurred_at, ingested_at, "
                "producer_agent_id, producer_adapter, producer_adapter_version, project_id, task_id, session_id, seq, "
                "parent_event_id, context_json, payload_json, artifacts_json, redaction_json, source, completeness, event_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.external_event_id,
                    event.schema_version,
                    event.event_type.value,
                    event.occurred_at.isoformat(),
                    now,
                    event.producer.agent_id,
                    event.producer.adapter,
                    event.producer.adapter_version,
                    event.project_id,
                    event.task_id,
                    event.session_id,
                    event.seq,
                    event.parent_event_id,
                    json_text(event_data["context"]),
                    json_text(event_data["payload"]),
                    json_text(event_data["artifacts"]),
                    json_text(event_data["redaction"]),
                    event.source.value,
                    event.completeness.value,
                    event_hash,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # A concurrent writer may have won after the identity check.
            existing = self._existing_by_identity(conn, event)
            if existing and self._same_hash(existing, event_hash):
                job = conn.execute(
                    "SELECT job_id FROM outbox WHERE job_type='event.ingested' AND dedupe_key=?",
                    (str(existing["event_id"]),),
                ).fetchone()
                return IngestResult(
                    status="duplicate",
                    event_id=str(existing["event_id"]),
                    accepted_seq=int(existing["seq"]),
                    outbox_job_id=str(job[0]) if job else None,
                    duplicate_of=str(existing["event_id"]),
                )
            raise ConflictError("event violates a uniqueness or relationship constraint") from exc

        job_id = str(uuid.uuid4())
        payload = {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "project_id": event.project_id,
            "task_id": event.task_id,
            "session_id": event.session_id,
        }
        conn.execute(
            "INSERT INTO outbox(job_id, job_type, aggregate_id, dedupe_key, payload_json, status, attempts, "
            "available_at, max_attempts, created_at, updated_at) VALUES (?, 'event.ingested', ?, ?, ?, 'pending', 0, ?, 8, ?, ?)",
            (job_id, event.event_id, event.event_id, json_text(payload), now, now, now),
        )
        conn.execute(
            "INSERT INTO event_fts(event_id, project_id, task_id, text) VALUES (?, ?, ?, ?)",
            (event.event_id, event.project_id, event.task_id, self._event_search_text(event)),
        )
        for artifact_id, ordinal in artifact_links:
            conn.execute(
                "INSERT INTO event_artifacts(event_id, artifact_id, ordinal) VALUES (?, ?, ?)",
                (event.event_id, artifact_id, ordinal),
            )
        if event.session_id:
            if event.event_type == EventType.SESSION_ENDED:
                conn.execute(
                    "UPDATE sessions SET status='ended', ended_at=?, last_seq=MAX(last_seq, ?) WHERE session_id=?",
                    (event.occurred_at.isoformat(), event.seq, event.session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET last_seq=MAX(last_seq, ?) WHERE session_id=?",
                    (event.seq, event.session_id),
                )
        return IngestResult(
            status="accepted",
            event_id=event.event_id,
            accepted_seq=event.seq,
            outbox_job_id=job_id,
        )

    def ingest(self, event: EventEnvelope) -> IngestResult:
        """Persist one event atomically and enqueue one deduplicated job."""

        with self.db.transaction() as conn:
            return self._ingest_with_connection(conn, event)

    def ingest_many(
        self, events: Iterable[EventEnvelope], *, atomic: bool = True
    ) -> list[IngestResult]:
        """Persist a batch in one transaction by default.

        A conflict aborts the atomic batch; callers that need per-record
        diagnostics can request ``atomic=False`` (the JSONL replay service
        uses a bounded batch with an individual fallback).
        """

        normalized = list(events)
        if not normalized:
            return []
        if not atomic:
            return [self.ingest(event) for event in normalized]
        results: list[IngestResult] = []
        with self.db.transaction() as conn:
            for event in normalized:
                results.append(self._ingest_with_connection(conn, event))
        return results

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": row["schema_version"],
            "event_id": row["event_id"],
            "external_event_id": row["external_event_id"],
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"],
            "ingested_at": row["ingested_at"],
            "producer": {
                "agent_id": row["producer_agent_id"],
                "adapter": row["producer_adapter"],
                "adapter_version": row["producer_adapter_version"],
            },
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "seq": row["seq"],
            "parent_event_id": row["parent_event_id"],
            "context": json_value(row["context_json"], {}),
            "payload": json_value(row["payload_json"], {}),
            "artifacts": json_value(row["artifacts_json"], []),
            "redaction": json_value(row["redaction_json"], {}),
            "source": row["source"],
            "completeness": row["completeness"],
        }

    def timeline(
        self, task_id: str, *, session_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        self.db.ensure_initialized()
        limit = max(1, min(int(limit), 5000))
        with self.db.connection() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM events WHERE task_id=? AND session_id=? ORDER BY seq, occurred_at LIMIT ?",
                    (task_id, session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events WHERE task_id=? ORDER BY occurred_at, seq LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def get_agent_memory_cycle(
        self,
        *,
        source_system: str,
        source_thread_id: str,
        project_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """Return the durable cursor for one external agent conversation."""

        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_memory_cycles "
                "WHERE source_system=? AND source_thread_id=? AND project_id=? AND session_id=?",
                (source_system, source_thread_id, project_id, session_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "cycle_id": str(row["cycle_id"]),
                "source_system": str(row["source_system"]),
                "source_thread_id": str(row["source_thread_id"]),
                "project_id": str(row["project_id"]),
                "task_id": str(row["task_id"]),
                "session_id": str(row["session_id"]),
                "status": str(row["status"]),
                "cursor": row["cursor"],
                "prompt_count": int(row["prompt_count"]),
                "last_event_seq": int(row["last_event_seq"]),
                "metadata": json_value(row["metadata_json"], {}),
                "opened_at": str(row["opened_at"]),
                "updated_at": str(row["updated_at"]),
                "closed_at": row["closed_at"],
            }

    def upsert_agent_memory_cycle(
        self,
        *,
        cycle_id: str,
        source_system: str,
        source_thread_id: str,
        project_id: str,
        task_id: str,
        session_id: str,
        status: str,
        cursor: str | None,
        prompt_count: int,
        last_event_seq: int,
        metadata: Mapping[str, Any] | None = None,
        opened_at: str | None = None,
        updated_at: str | None = None,
        closed_at: str | None = None,
    ) -> dict[str, Any]:
        """Create or advance a cycle cursor without duplicating a conversation."""

        now = updated_at or iso_now()
        opened = opened_at or now
        metadata_json = json_text(dict(metadata or {}))
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO agent_memory_cycles "
                "(cycle_id,source_system,source_thread_id,project_id,task_id,session_id,status,cursor,"
                "prompt_count,last_event_seq,metadata_json,opened_at,updated_at,closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_system,source_thread_id,project_id,session_id) DO UPDATE SET "
                "cycle_id=excluded.cycle_id, task_id=excluded.task_id, status=excluded.status, "
                "cursor=excluded.cursor, prompt_count=excluded.prompt_count, "
                "last_event_seq=excluded.last_event_seq, metadata_json=excluded.metadata_json, "
                "updated_at=excluded.updated_at, closed_at=excluded.closed_at",
                (
                    cycle_id,
                    source_system,
                    source_thread_id,
                    project_id,
                    task_id,
                    session_id,
                    status,
                    cursor,
                    max(0, int(prompt_count)),
                    max(-1, int(last_event_seq)),
                    metadata_json,
                    opened,
                    now,
                    closed_at,
                ),
            )
        result = self.get_agent_memory_cycle(
            source_system=source_system,
            source_thread_id=source_thread_id,
            project_id=project_id,
            session_id=session_id,
        )
        if result is None:  # pragma: no cover - defensive guard for a failed transaction
            raise RuntimeError("agent memory cycle was not persisted")
        return result

    @staticmethod
    def _maintenance_from_row(row: sqlite3.Row) -> dict[str, Any]:
        attempts = int(row["attempts"])
        max_attempts = int(row["max_attempts"])
        status = str(row["status"])
        return {
            "maintenance_id": str(row["maintenance_id"]),
            "cycle_id": str(row["cycle_id"]),
            "project_id": str(row["project_id"]),
            "task_id": str(row["task_id"]),
            "session_id": str(row["session_id"]),
            "source_thread_id": str(row["source_thread_id"]),
            "request_turn_id": str(row["request_turn_id"]),
            "input_hash": str(row["input_hash"]),
            "status": status,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "retry_allowed": status != "completed" and attempts < max_attempts,
            "exhausted": status != "completed" and attempts >= max_attempts,
            "provider": str(row["provider"]),
            "model": row["model"],
            "note_count": int(row["note_count"]),
            "candidate_count": int(row["candidate_count"]),
            "last_error": row["last_error"],
            "metadata": json_value(row["metadata_json"], {}),
            "requested_at": str(row["requested_at"]),
            "updated_at": str(row["updated_at"]),
            "completed_at": row["completed_at"],
        }

    def get_agent_memory_maintenance(
        self, *, cycle_id: str, input_hash: str
    ) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_memory_maintenance WHERE cycle_id=? AND input_hash=?",
                (cycle_id, input_hash),
            ).fetchone()
            return self._maintenance_from_row(row) if row is not None else None

    def latest_agent_memory_maintenance(self, *, cycle_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_memory_maintenance WHERE cycle_id=? "
                "ORDER BY updated_at DESC, requested_at DESC LIMIT 1",
                (cycle_id,),
            ).fetchone()
            return self._maintenance_from_row(row) if row is not None else None

    def request_agent_memory_maintenance(
        self,
        *,
        maintenance_id: str,
        cycle_id: str,
        project_id: str,
        task_id: str,
        session_id: str,
        source_thread_id: str,
        request_turn_id: str,
        input_hash: str,
        max_attempts: int = 2,
        provider: str = "agent",
        model: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or retry one evidence-addressed maintenance request."""

        now = iso_now()
        maximum = max(1, int(max_attempts))
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM agent_memory_maintenance WHERE cycle_id=? AND input_hash=?",
                (cycle_id, input_hash),
            ).fetchone()
            if existing is not None:
                current = self._maintenance_from_row(existing)
                maximum = max(maximum, int(current["max_attempts"]))
                if current["status"] == "completed" or current["attempts"] >= maximum:
                    return current
                attempts = int(current["attempts"]) + 1
                conn.execute(
                    "UPDATE agent_memory_maintenance SET status='pending', attempts=?, max_attempts=?, "
                    "request_turn_id=?, provider=?, model=COALESCE(?,model), last_error=NULL, "
                    "metadata_json=?, updated_at=? WHERE maintenance_id=?",
                    (
                        attempts,
                        maximum,
                        request_turn_id,
                        provider,
                        model,
                        json_text(dict(metadata or current["metadata"])),
                        now,
                        current["maintenance_id"],
                    ),
                )
                maintenance_id = str(current["maintenance_id"])
            else:
                conn.execute(
                    "UPDATE agent_memory_maintenance SET status='superseded', updated_at=? "
                    "WHERE cycle_id=? AND status='pending' AND input_hash<>?",
                    (now, cycle_id, input_hash),
                )
                conn.execute(
                    "INSERT INTO agent_memory_maintenance "
                    "(maintenance_id,cycle_id,project_id,task_id,session_id,source_thread_id,"
                    "request_turn_id,input_hash,status,attempts,max_attempts,provider,model,"
                    "metadata_json,requested_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?, 'pending',1,?,?,?,?,?,?)",
                    (
                        maintenance_id,
                        cycle_id,
                        project_id,
                        task_id,
                        session_id,
                        source_thread_id,
                        request_turn_id,
                        input_hash,
                        maximum,
                        provider,
                        model,
                        json_text(dict(metadata or {})),
                        now,
                        now,
                    ),
                )
        result = self.get_agent_memory_maintenance(cycle_id=cycle_id, input_hash=input_hash)
        if result is None:  # pragma: no cover - transaction guard
            raise RuntimeError("agent memory maintenance request was not persisted")
        return result

    def complete_agent_memory_maintenance(
        self,
        *,
        cycle_id: str,
        input_hash: str,
        model: str,
        note_count: int,
        candidate_count: int,
    ) -> dict[str, Any] | None:
        now = iso_now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE agent_memory_maintenance SET status='completed', model=?, note_count=?, "
                "candidate_count=?, last_error=NULL, updated_at=?, completed_at=? "
                "WHERE cycle_id=? AND input_hash=?",
                (
                    model,
                    max(0, int(note_count)),
                    max(0, int(candidate_count)),
                    now,
                    now,
                    cycle_id,
                    input_hash,
                ),
            )
        return self.get_agent_memory_maintenance(cycle_id=cycle_id, input_hash=input_hash)

    def fail_agent_memory_maintenance(
        self, *, cycle_id: str, input_hash: str, error: str
    ) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE agent_memory_maintenance SET status='failed', last_error=?, updated_at=? "
                "WHERE cycle_id=? AND input_hash=? AND status<>'completed'",
                (str(error)[:2000], iso_now(), cycle_id, input_hash),
            )
        return self.get_agent_memory_maintenance(cycle_id=cycle_id, input_hash=input_hash)

    def fail_pending_agent_memory_maintenance(
        self, *, cycle_id: str, error: str
    ) -> dict[str, Any] | None:
        latest = self.latest_agent_memory_maintenance(cycle_id=cycle_id)
        if latest is None or latest["status"] != "pending":
            return latest
        return self.fail_agent_memory_maintenance(
            cycle_id=cycle_id,
            input_hash=str(latest["input_hash"]),
            error=error,
        )

    def list_agent_memory_maintenance(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        updated_since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if updated_since:
            clauses.append("updated_at>=?")
            params.append(updated_since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_memory_maintenance {where} "
                "ORDER BY updated_at DESC, requested_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 5000))),
            ).fetchall()
            return [self._maintenance_from_row(row) for row in rows]

    def agent_memory_maintenance_count(self) -> dict[str, int]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS n FROM agent_memory_maintenance GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "completed": 0, "failed": 0, "superseded": 0}
        counts.update({str(row["status"]): int(row["n"]) for row in rows})
        return counts

    def record_memory_card_feedback(
        self,
        *,
        feedback_id: str,
        feedback_key: str,
        project_id: str,
        task_id: str,
        session_id: str,
        card_id: str,
        turn_id: str,
        feedback_type: str,
        outcome: str = "unknown",
        used: bool = False,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> bool:
        """Record idempotent evidence about whether a route card was useful."""

        with self.db.transaction() as conn:
            result = conn.execute(
                "INSERT OR IGNORE INTO memory_card_feedback "
                "(feedback_id,feedback_key,project_id,task_id,session_id,card_id,turn_id,"
                "feedback_type,outcome,used,reason,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    feedback_id,
                    feedback_key,
                    project_id,
                    task_id,
                    session_id,
                    card_id,
                    turn_id,
                    feedback_type,
                    outcome,
                    1 if used else 0,
                    reason,
                    json_text(dict(metadata or {})),
                    created_at or iso_now(),
                ),
            )
            return bool(result.rowcount)

    def list_tasks(
        self,
        *,
        project_id: str | None = None,
        limit: int = 1000,
        updated_since: str | None = None,
        min_events: int = 0,
    ) -> list[dict[str, Any]]:
        """List canonical tasks for bulk extraction and inspection."""

        limit = max(1, min(int(limit), 100_000))
        min_events = max(0, int(min_events))
        with self.db.connection() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            if project_id:
                clauses.append("t.project_id=?")
                params.append(project_id)
            if updated_since:
                clauses.append("t.updated_at>=?")
                params.append(updated_since)
            if min_events:
                clauses.append("(SELECT COUNT(*) FROM events e WHERE e.task_id=t.task_id)>=?")
                params.append(min_events)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                "SELECT t.task_id,t.project_id,t.title,t.status,t.created_at,t.updated_at FROM tasks t "
                f"{where} ORDER BY t.updated_at DESC, t.task_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [
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

    def complete_outbox_for_task(self, task_id: str, *, job_type: str = "event.ingested") -> int:
        """Acknowledge pending event jobs after a successful task-level run.

        Bulk extraction operates once per task instead of once per event.  This
        helper keeps the durable event outbox consistent without replaying the
        same task thousands of times; leased jobs owned by another worker are
        intentionally left untouched.
        """

        now = iso_now()
        with self.db.transaction() as conn:
            result = conn.execute(
                "UPDATE outbox SET status='completed', lease_until=NULL, updated_at=? "
                "WHERE job_type=? AND status IN ('pending','retry') "
                "AND json_extract(payload_json, '$.task_id')=?",
                (now, job_type, task_id),
            )
            return int(result.rowcount)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return self._event_from_row(row) if row else None

    def export_events(
        self, *, task_id: str | None = None, project_id: str | None = None, limit: int = 100_000
    ) -> list[dict[str, Any]]:
        """Export canonical envelopes for backup, migration, and replay."""

        limit = max(1, min(int(limit), 1_000_000))
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM events {where} ORDER BY occurred_at, seq LIMIT ?",
                (*params, limit),
            ).fetchall()
        records = []
        for row in rows:
            record = self._event_from_row(row)
            record.pop("ingested_at", None)
            records.append(record)
        return records

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        logical_project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 200))
        logical_raw_ids = raw_project_ids_for_logical(self.db, logical_project_id)
        with self.db.connection() as conn:
            scope_sql = ""
            scope_params: list[Any] = []
            if project_id:
                scope_sql += " AND f.project_id=?"
                scope_params.append(project_id)
            if logical_project_id:
                if logical_raw_ids:
                    placeholders = ",".join("?" for _ in logical_raw_ids)
                    scope_sql += f" AND f.project_id IN ({placeholders})"
                    scope_params.extend(logical_raw_ids)
                else:
                    scope_sql += " AND 1=0"
            try:
                rows = conn.execute(
                    "SELECT e.* FROM event_fts f JOIN events e ON e.event_id=f.event_id "
                    "WHERE event_fts MATCH ?" + scope_sql + " ORDER BY e.occurred_at DESC LIMIT ?",
                    (query, *scope_params, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # Treat punctuation-heavy user input as a literal phrase.
                literal = '"' + query.replace('"', '""') + '"'
                rows = conn.execute(
                    "SELECT e.* FROM event_fts f JOIN events e ON e.event_id=f.event_id "
                    "WHERE event_fts MATCH ?" + scope_sql + " ORDER BY e.occurred_at DESC LIMIT ?",
                    (literal, *scope_params, limit),
                ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def rebuild_search_index(self) -> int:
        """Rebuild the optional FTS projection entirely from canonical events."""

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM event_fts")
            rows = conn.execute("SELECT * FROM events ORDER BY occurred_at, seq").fetchall()
            for row in rows:
                text = " ".join(
                    [
                        str(row["event_type"]),
                        str(row["project_id"]),
                        str(row["task_id"]),
                        str(row["context_json"]),
                        str(row["payload_json"]),
                        str(row["artifacts_json"]),
                    ]
                )
                conn.execute(
                    "INSERT INTO event_fts(event_id, project_id, task_id, text) VALUES (?, ?, ?, ?)",
                    (row["event_id"], row["project_id"], row["task_id"], text),
                )
            return len(rows)

    def outbox_count(self) -> dict[str, int]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status"
            ).fetchall()
            result = {str(row["status"]): int(row["n"]) for row in rows}
            return {
                key: result.get(key, 0)
                for key in ("pending", "processing", "retry", "completed", "dead")
            }

    def list_outbox(self, *, status: str | None = None, limit: int = 100) -> list[OutboxJob]:
        limit = max(1, min(int(limit), 1000))
        with self.db.connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE status=? ORDER BY created_at LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outbox ORDER BY created_at LIMIT ?", (limit,)
                ).fetchall()
            return [OutboxJob.from_row(row) for row in rows]

    def claim_outbox(
        self,
        *,
        limit: int = 20,
        lease_seconds: int = 60,
        job_type: str | None = None,
    ) -> list[OutboxJob]:
        now = utc_now()
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        limit = max(1, min(int(limit), 100))
        with self.db.transaction() as conn:
            # Expired leases are safely returned to the retry queue.
            conn.execute(
                "UPDATE outbox SET status=CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'retry' END, "
                "available_at=?, lease_until=NULL, updated_at=? "
                "WHERE status='processing' AND lease_until IS NOT NULL AND lease_until <= ?",
                (now_text, now_text, now_text),
            )
            if job_type:
                rows = conn.execute(
                    "SELECT job_id FROM outbox WHERE status IN ('pending', 'retry') "
                    "AND available_at <= ? AND job_type=? ORDER BY created_at LIMIT ?",
                    (now_text, job_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT job_id FROM outbox WHERE status IN ('pending', 'retry') AND available_at <= ? "
                    "ORDER BY created_at LIMIT ?",
                    (now_text, limit),
                ).fetchall()
            ids = [str(row[0]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE outbox SET status='processing', attempts=attempts+1, lease_until=?, updated_at=? "
                f"WHERE job_id IN ({placeholders})",
                (lease_until, now_text, *ids),
            )
            claimed = conn.execute(
                f"SELECT * FROM outbox WHERE job_id IN ({placeholders}) ORDER BY created_at", ids
            ).fetchall()
            return [OutboxJob.from_row(row) for row in claimed]

    def claim_outbox_for_task(
        self,
        task_id: str,
        *,
        lease_seconds: int = 120,
        job_type: str = "event.ingested",
    ) -> OutboxJob | None:
        """Lease one representative job for a task-level projection run."""

        now = utc_now()
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE outbox SET status=CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'retry' END, "
                "available_at=?, lease_until=NULL, updated_at=? "
                "WHERE status='processing' AND lease_until IS NOT NULL AND lease_until <= ?",
                (now_text, now_text, now_text),
            )
            row = conn.execute(
                "SELECT job_id FROM outbox WHERE status IN ('pending','retry') "
                "AND available_at<=? AND job_type=? "
                "AND json_extract(payload_json,'$.task_id')=? "
                "ORDER BY created_at,job_id LIMIT 1",
                (now_text, job_type, task_id),
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            conn.execute(
                "UPDATE outbox SET status='processing',attempts=attempts+1,lease_until=?,updated_at=? "
                "WHERE job_id=? AND status IN ('pending','retry')",
                (lease_until, now_text, job_id),
            )
            leased = conn.execute("SELECT * FROM outbox WHERE job_id=?", (job_id,)).fetchone()
            return OutboxJob.from_row(leased) if leased is not None else None

    def fail_outbox_for_task(
        self,
        task_id: str,
        error: str,
        *,
        job_type: str = "event.ingested",
        dead: bool = False,
        retry_delay_seconds: int = 60,
    ) -> int:
        """Park every unleased task job after a task-level extraction failure."""

        now = utc_now()
        now_text = now.isoformat()
        available = (now + timedelta(seconds=max(0, int(retry_delay_seconds)))).isoformat()
        status = "dead" if dead else "retry"
        with self.db.transaction() as conn:
            result = conn.execute(
                "UPDATE outbox SET status=?,available_at=?,lease_until=NULL,last_error=?,updated_at=? "
                "WHERE job_type=? AND status IN ('pending','retry') "
                "AND json_extract(payload_json,'$.task_id')=?",
                (status, available, str(error)[:2000], now_text, job_type, task_id),
            )
            return int(result.rowcount)

    def complete_outbox(self, job_id: str) -> bool:
        now = iso_now()
        with self.db.transaction() as conn:
            result = conn.execute(
                "UPDATE outbox SET status='completed', lease_until=NULL, updated_at=? "
                "WHERE job_id=? AND status='processing'",
                (now, job_id),
            )
            return result.rowcount == 1

    def release_outbox(self, job_id: str) -> bool:
        """Return a leased job to pending without pretending projection succeeded."""

        now = iso_now()
        with self.db.transaction() as conn:
            result = conn.execute(
                "UPDATE outbox SET status='pending',lease_until=NULL,updated_at=? "
                "WHERE job_id=? AND status='processing'",
                (now, job_id),
            )
            return result.rowcount == 1

    def fail_outbox(
        self,
        job_id: str,
        error: str,
        *,
        retry_at: str | None = None,
        retry_delay_seconds: int | None = None,
        dead: bool = False,
    ) -> bool:
        now = iso_now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM outbox WHERE job_id=? AND status='processing'",
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            next_status = "dead" if dead or attempts >= max_attempts else "retry"
            if retry_at:
                available = retry_at
            elif retry_delay_seconds is not None:
                available = (utc_now() + timedelta(seconds=max(0, retry_delay_seconds))).isoformat()
            else:
                # Exponential backoff is deterministic and bounded. Attempts
                # is incremented at claim time, so the first retry waits 2s.
                delay = min(3600, 2 ** max(1, min(attempts, 10)))
                available = (utc_now() + timedelta(seconds=delay)).isoformat()
            result = conn.execute(
                "UPDATE outbox SET status=?, available_at=?, lease_until=NULL, last_error=?, updated_at=? "
                "WHERE job_id=? AND status='processing'",
                (next_status, available, error[:2000], now, job_id),
            )
            return result.rowcount == 1

    def health(self, *, check_integrity: bool = True) -> dict[str, Any]:
        self.db.ensure_initialized()
        integrity = self.db.integrity_check() if check_integrity else "not_run"
        with self.db.connection() as conn:
            counts = {
                "projects": int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]),
                "tasks": int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
                "sessions": int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]),
                "agent_memory_cycles": int(
                    conn.execute("SELECT COUNT(*) FROM agent_memory_cycles").fetchone()[0]
                ),
                "agent_memory_maintenance": int(
                    conn.execute("SELECT COUNT(*) FROM agent_memory_maintenance").fetchone()[0]
                ),
                "memory_card_feedback": int(
                    conn.execute("SELECT COUNT(*) FROM memory_card_feedback").fetchone()[0]
                ),
                "events": int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
                "artifacts": int(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]),
                "extraction_runs": int(
                    conn.execute("SELECT COUNT(*) FROM extraction_runs").fetchone()[0]
                ),
                "memory_candidates": int(
                    conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
                ),
                "memory_cards": int(
                    conn.execute("SELECT COUNT(*) FROM memory_cards").fetchone()[0]
                ),
                "memory_card_versions": int(
                    conn.execute("SELECT COUNT(*) FROM memory_card_versions").fetchone()[0]
                ),
                "memory_card_evidence": int(
                    conn.execute("SELECT COUNT(*) FROM memory_card_evidence").fetchone()[0]
                ),
                "memory_card_bindings": int(
                    conn.execute("SELECT COUNT(*) FROM memory_card_bindings").fetchone()[0]
                ),
                "memory_card_links": int(
                    conn.execute("SELECT COUNT(*) FROM memory_card_links").fetchone()[0]
                ),
                "consolidation_decisions": int(
                    conn.execute("SELECT COUNT(*) FROM consolidation_decisions").fetchone()[0]
                ),
                "event_quality_evaluations": int(
                    conn.execute("SELECT COUNT(*) FROM event_quality_evaluations").fetchone()[0]
                ),
                "candidate_quality_reviews": int(
                    conn.execute("SELECT COUNT(*) FROM candidate_quality_reviews").fetchone()[0]
                ),
                "logical_projects": int(
                    conn.execute("SELECT COUNT(*) FROM logical_projects").fetchone()[0]
                ),
                "project_aliases": int(
                    conn.execute("SELECT COUNT(*) FROM project_aliases").fetchone()[0]
                ),
                "quality_runs": int(
                    conn.execute("SELECT COUNT(*) FROM quality_runs").fetchone()[0]
                ),
                "code_snapshots": int(
                    conn.execute("SELECT COUNT(*) FROM code_snapshots").fetchone()[0]
                ),
                "verification_runs": int(
                    conn.execute("SELECT COUNT(*) FROM verification_runs").fetchone()[0]
                ),
                "binding_verifications": int(
                    conn.execute("SELECT COUNT(*) FROM binding_verifications").fetchone()[0]
                ),
            }
            unresolved_parents = int(
                conn.execute(
                    "SELECT COUNT(*) FROM events child LEFT JOIN events parent "
                    "ON parent.event_id=child.parent_event_id "
                    "WHERE child.parent_event_id IS NOT NULL AND parent.event_id IS NULL"
                ).fetchone()[0]
            )
        return {
            "status": "ok" if integrity in {"ok", "not_run"} else "degraded",
            "database": str(Path(self.db.path).resolve()),
            "journal_mode": self.db.journal_mode(),
            "schema_version": self.db.migration_version(),
            "integrity_check": integrity,
            "integrity_checked": check_integrity,
            "counts": counts,
            "unresolved_parent_links": unresolved_parents,
            "outbox": self.outbox_count(),
            "agent_memory_maintenance": self.agent_memory_maintenance_count(),
        }
