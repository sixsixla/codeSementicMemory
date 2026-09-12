"""SQLite persistence for Phase 2A extraction runs and candidate memories."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..storage.database import Database
from ..storage.repository import json_text, json_value
from ..storage.scope import raw_project_ids_for_logical
from .context import AssembledContext
from .models import Candidate, ExtractionBatch


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class RunStart:
    run_id: str
    status: str
    created: bool
    execute: bool
    attempts: int
    input_hash: str


@dataclass(frozen=True)
class StoredBatch:
    run_id: str
    candidate_count: int
    result_hash: str


class ExtractionStore:
    """Canonical candidate/run writer and graph projection reader."""

    def __init__(self, database: Database):
        self.db = database
        self.db.ensure_initialized()

    def begin_run(
        self,
        context: AssembledContext,
        *,
        provider: str,
        model: str,
        extractor_version: str,
        prompt_version: str,
        schema_version: str,
        force: bool = False,
    ) -> RunStart:
        now = _now()
        run_id = str(uuid.uuid4())
        input_json = json_text(context.as_dict())
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT run_id,status,attempts FROM extraction_runs "
                "WHERE task_id=? AND input_hash=? AND extractor_version=? AND prompt_version=? AND schema_version=?",
                (
                    context.task_id,
                    context.input_hash,
                    extractor_version,
                    prompt_version,
                    schema_version,
                ),
            ).fetchone()
            if row:
                status = str(row["status"])
                attempts = int(row["attempts"])
                existing_id = str(row["run_id"])
                if status == "succeeded" and not force:
                    return RunStart(existing_id, status, False, False, attempts, context.input_hash)
                if status == "running" and not force:
                    return RunStart(existing_id, status, False, False, attempts, context.input_hash)
                if status == "dead" and not force:
                    # Dead-lettered runs require an explicit operator retry so
                    # a broken provider cannot spin forever on every outbox
                    # delivery attempt.
                    return RunStart(existing_id, status, False, False, attempts, context.input_hash)
                next_attempts = attempts + 1
                conn.execute(
                    "UPDATE extraction_runs SET status='running', attempts=?, error=NULL, updated_at=?, "
                    "provider=?, model=? WHERE run_id=?",
                    (next_attempts, now, provider, model, existing_id),
                )
                return RunStart(
                    existing_id, "running", False, True, next_attempts, context.input_hash
                )
            conn.execute(
                "INSERT INTO extraction_runs(run_id,project_id,task_id,session_id,input_hash,"
                "extractor_version,prompt_version,schema_version,provider,model,status,input_json,attempts,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'running', ?, 1, ?, ?)",
                (
                    run_id,
                    context.project_id,
                    context.task_id,
                    context.session_id,
                    context.input_hash,
                    extractor_version,
                    prompt_version,
                    schema_version,
                    provider,
                    model,
                    input_json,
                    now,
                    now,
                ),
            )
            return RunStart(run_id, "running", True, True, 1, context.input_hash)

    def record_success(
        self,
        run: RunStart,
        context: AssembledContext,
        batch: ExtractionBatch,
    ) -> StoredBatch:
        now = _now()
        stored_count = 0
        stored_candidates: list[dict[str, Any]] = []
        with self.db.transaction() as conn:
            # Keep the last successful projection visible while a forced retry
            # is running.  If those candidates have already been consolidated,
            # foreign keys intentionally prevent deleting them: candidate and
            # card provenance is append-only.  In that case retain the old
            # generation and write the replacement with collision-safe ids;
            # otherwise an unreferenced projection can be replaced in place.
            prior_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT candidate_id FROM memory_candidates WHERE run_id=?",
                    (run.run_id,),
                ).fetchall()
            ]
            protected = False
            if prior_ids:
                placeholders = ",".join("?" for _ in prior_ids)
                protected = bool(
                    conn.execute(
                        "SELECT 1 FROM memory_card_evidence WHERE candidate_id IN ("
                        + placeholders
                        + ") LIMIT 1",
                        prior_ids,
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM consolidation_decisions WHERE candidate_id IN ("
                        + placeholders
                        + ") LIMIT 1",
                        prior_ids,
                    ).fetchone()
                )
            if not protected:
                conn.execute(
                    "DELETE FROM memory_candidate_fts WHERE candidate_id IN "
                    "(SELECT candidate_id FROM memory_candidates WHERE run_id=?)",
                    (run.run_id,),
                )
                conn.execute("DELETE FROM memory_candidates WHERE run_id=?", (run.run_id,))
            for candidate in batch.candidates:
                candidate_id = self._available_candidate_id(conn, candidate, run.run_id)
                candidate_payload = candidate.model_dump(mode="json")
                candidate_payload["candidate_id"] = candidate_id
                stored_candidates.append(candidate_payload)
                conn.execute(
                    "INSERT INTO memory_candidates(candidate_id,run_id,project_id,task_id,session_id,kind,statement,"
                    "aliases_json,bindings_json,evidence_event_ids_json,confidence,uncertainty,lifecycle_hint,"
                    "relation_hints_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        run.run_id,
                        batch.project_id,
                        batch.task_id,
                        batch.session_id,
                        candidate.kind.value,
                        candidate.statement,
                        json_text(candidate.aliases),
                        json_text(
                            [binding.model_dump(mode="json") for binding in candidate.bindings]
                        ),
                        json_text(candidate.evidence_event_ids),
                        candidate.confidence,
                        candidate.uncertainty,
                        candidate.lifecycle_hint.value,
                        json_text(candidate.relation_hints),
                        now,
                        now,
                    ),
                )
                # A forced retry that preserves a consolidated candidate is a
                # new observation, not an in-place mutation.  Retain an
                # explicit candidate-level supersede edge when the provider
                # reused the original id so graph consumers can explain why
                # both generations exist.
                if protected and candidate.candidate_id in prior_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_candidate_links(candidate_id,target_type,target_id,relation_type,metadata_json) "
                        "VALUES (?,?,?,?,?)",
                        (candidate_id, "candidate", candidate.candidate_id, "supersedes", "{}"),
                    )
                for ordinal, event_id in enumerate(candidate.evidence_event_ids):
                    conn.execute(
                        "INSERT INTO memory_candidate_evidence(candidate_id,event_id,ordinal) VALUES (?,?,?)",
                        (candidate_id, event_id, ordinal),
                    )
                    conn.execute(
                        "INSERT INTO memory_candidate_links(candidate_id,target_type,target_id,relation_type,metadata_json) "
                        "VALUES (?,?,?,?,?)",
                        (candidate_id, "event", event_id, "supports", "{}"),
                    )
                for binding in candidate.bindings:
                    if binding.path:
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_candidate_links(candidate_id,target_type,target_id,relation_type,metadata_json) "
                            "VALUES (?,?,?,?,?)",
                            (candidate_id, "file", binding.path, binding.role.value, "{}"),
                        )
                    if binding.symbol or binding.qualified_symbol:
                        symbol = binding.qualified_symbol or binding.symbol
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_candidate_links(candidate_id,target_type,target_id,relation_type,metadata_json) "
                            "VALUES (?,?,?,?,?)",
                            (candidate_id, "symbol", symbol, binding.role.value, "{}"),
                        )
                for hint in candidate.relation_hints:
                    if not isinstance(hint, dict):
                        continue
                    target_id = hint.get("target_id")
                    target_type = hint.get("target_type", "candidate")
                    relation = hint.get("relation", "related_to")
                    if target_id and target_type in {
                        "event",
                        "candidate",
                        "task",
                        "file",
                        "symbol",
                    }:
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_candidate_links(candidate_id,target_type,target_id,relation_type,metadata_json) "
                            "VALUES (?,?,?,?,?)",
                            (
                                candidate_id,
                                str(target_type),
                                str(target_id),
                                str(relation),
                                json_text(hint),
                            ),
                        )
                search_text = " ".join(
                    [
                        candidate.kind.value,
                        candidate.statement,
                        *candidate.aliases,
                        *[
                            value
                            for binding in candidate.bindings
                            for value in (binding.path, binding.symbol, binding.qualified_symbol)
                            if value
                        ],
                    ]
                )
                conn.execute(
                    "INSERT INTO memory_candidate_fts(candidate_id,project_id,task_id,text) VALUES (?,?,?,?)",
                    (candidate_id, batch.project_id, batch.task_id, search_text),
                )
                stored_count += 1
            # Persist the normalized ids actually used by SQLite.  This keeps
            # ``extraction_runs.result_json`` aligned with candidate rows when
            # a protected forced retry required collision-safe suffixes.
            result_payload = batch.model_dump(mode="json")
            result_payload["candidates"] = stored_candidates
            result_json = json_text(result_payload)
            result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
            conn.execute(
                "UPDATE extraction_runs SET status='succeeded', result_json=?, result_hash=?, error=NULL, updated_at=? WHERE run_id=?",
                (result_json, result_hash, now, run.run_id),
            )
        return StoredBatch(run.run_id, stored_count, result_hash)

    def record_failure(self, run_id: str, error: str, *, dead: bool = False) -> bool:
        now = _now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT attempts FROM extraction_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False
            status = "dead" if dead else "retry"
            return (
                conn.execute(
                    "UPDATE extraction_runs SET status=?, error=?, updated_at=? WHERE run_id=?",
                    (status, error[:4000], now, run_id),
                ).rowcount
                == 1
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM extraction_runs WHERE run_id=?", (run_id,)).fetchone()
            return self._run_dict(row) if row else None

    def list_runs(self, *, task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.db.connection() as conn:
            if task_id:
                rows = conn.execute(
                    "SELECT * FROM extraction_runs WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM extraction_runs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._run_dict(row) for row in rows]

    def list_candidates(
        self,
        *,
        project_id: str | None = None,
        logical_project_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        include_quarantine: bool = True,
    ) -> list[dict[str, Any]]:
        """List candidates, optionally hiding quality-quarantined rows.

        The low-level store keeps its historical audit-friendly default of
        including every immutable candidate.  API/CLI retrieval surfaces pass
        ``include_quarantine=False`` for the product's conservative default;
        extraction and consolidation internals explicitly keep the full set.
        """

        limit = max(1, min(int(limit), 1000))
        clauses = []
        params: list[Any] = []
        logical_raw_ids = raw_project_ids_for_logical(self.db, logical_project_id)
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if logical_project_id:
            if logical_raw_ids:
                placeholders = ",".join("?" for _ in logical_raw_ids)
                clauses.append(f"project_id IN ({placeholders})")
                params.extend(logical_raw_ids)
            else:
                clauses.append("1=0")
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if not include_quarantine:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM candidate_quality_reviews cq "
                "WHERE cq.candidate_id=memory_candidates.candidate_id AND cq.decision='quarantine')"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM memory_candidates {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            result = [self._candidate_dict(row, conn) for row in rows]
            if logical_project_id:
                for item in result:
                    item["logical_project_id"] = logical_project_id
                    item["logical_scope_raw_project_ids"] = logical_raw_ids
            return result

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """Return one candidate with bounded source-event excerpts."""

        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                return None
            result = self._candidate_dict(row, conn)
            evidence: list[dict[str, Any]] = []
            for event_id in result["evidence_event_ids"]:
                event = conn.execute(
                    "SELECT event_id,event_type,occurred_at,payload_json,source,completeness FROM events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if event is None:
                    continue
                payload = json_value(event["payload_json"], {})
                if isinstance(payload, dict):
                    text = str(
                        payload.get("text")
                        or payload.get("message")
                        or payload.get("summary")
                        or ""
                    )
                    if len(text) > 2000:
                        payload = {"text": text[:1999] + "…", "truncated": True}
                evidence.append(
                    {
                        "event_id": str(event["event_id"]),
                        "event_type": str(event["event_type"]),
                        "occurred_at": str(event["occurred_at"]),
                        "payload": payload,
                        "source": str(event["source"]),
                        "completeness": str(event["completeness"]),
                    }
                )
            result["evidence"] = evidence
            result["run"] = self.get_run(result["run_id"])
            return result

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        logical_project_id: str | None = None,
        task_id: str | None = None,
        limit: int = 20,
        include_quarantine: bool = True,
    ) -> list[dict[str, Any]]:
        """Search candidate FTS with an explicit quarantine audit switch."""

        query = query.strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 200))
        with self.db.connection() as conn:
            logical_raw_ids = raw_project_ids_for_logical(self.db, logical_project_id)
            scope_suffix = ""
            scope_params: list[Any] = []
            if project_id:
                scope_suffix += " AND c.project_id=?"
                scope_params.append(project_id)
            if logical_project_id:
                if logical_raw_ids:
                    placeholders = ",".join("?" for _ in logical_raw_ids)
                    scope_suffix += f" AND c.project_id IN ({placeholders})"
                    scope_params.extend(logical_raw_ids)
                else:
                    scope_suffix += " AND 1=0"
            quality_suffix = (
                ""
                if include_quarantine
                else (
                    " AND NOT EXISTS (SELECT 1 FROM candidate_quality_reviews cq "
                    "WHERE cq.candidate_id=c.candidate_id AND cq.decision='quarantine')"
                )
            )
            try:
                if task_id:
                    rows = conn.execute(
                        "SELECT c.* FROM memory_candidate_fts f JOIN memory_candidates c ON c.candidate_id=f.candidate_id "
                        "WHERE memory_candidate_fts MATCH ? AND f.task_id=?"
                        + scope_suffix
                        + quality_suffix
                        + " ORDER BY c.created_at DESC LIMIT ?",
                        (query, task_id, *scope_params, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT c.* FROM memory_candidate_fts f JOIN memory_candidates c ON c.candidate_id=f.candidate_id "
                        "WHERE memory_candidate_fts MATCH ?"
                        + scope_suffix
                        + quality_suffix
                        + " ORDER BY c.created_at DESC LIMIT ?",
                        (query, *scope_params, limit),
                    ).fetchall()
            except sqlite3.OperationalError:
                literal = '"' + query.replace('"', '""') + '"'
                if task_id:
                    rows = conn.execute(
                        "SELECT c.* FROM memory_candidate_fts f JOIN memory_candidates c ON c.candidate_id=f.candidate_id "
                        "WHERE memory_candidate_fts MATCH ? AND f.task_id=?"
                        + scope_suffix
                        + quality_suffix
                        + " ORDER BY c.created_at DESC LIMIT ?",
                        (literal, task_id, *scope_params, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT c.* FROM memory_candidate_fts f JOIN memory_candidates c ON c.candidate_id=f.candidate_id "
                        "WHERE memory_candidate_fts MATCH ?"
                        + scope_suffix
                        + quality_suffix
                        + " ORDER BY c.created_at DESC LIMIT ?",
                        (literal, *scope_params, limit),
                    ).fetchall()
            # unicode61 treats a contiguous Chinese phrase as one token, so a
            # short substring such as "归属" may legitimately miss FTS.  Keep
            # a literal substring fallback for explainable local search rather
            # than requiring an embedding model in the Phase 2A baseline.
            if not rows:
                if task_id:
                    quality_suffix = (
                        ""
                        if include_quarantine
                        else (
                            " AND NOT EXISTS (SELECT 1 FROM candidate_quality_reviews cq "
                            "WHERE cq.candidate_id=memory_candidates.candidate_id AND cq.decision='quarantine')"
                        )
                    )
                    rows = conn.execute(
                        "SELECT * FROM memory_candidates WHERE task_id=? AND "
                        "(instr(statement, ?) > 0 OR instr(aliases_json, ?) > 0 OR instr(bindings_json, ?) > 0)"
                        + scope_suffix.replace("c.project_id", "project_id")
                        + quality_suffix.replace("c.candidate_id", "candidate_id")
                        + " "
                        "ORDER BY created_at DESC LIMIT ?",
                        (task_id, query, query, query, *scope_params, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM memory_candidates WHERE "
                        "(instr(statement, ?) > 0 OR instr(aliases_json, ?) > 0 OR instr(bindings_json, ?) > 0)"
                        + scope_suffix.replace("c.project_id", "project_id").replace(
                            "c.candidate_id", "candidate_id"
                        )
                        + quality_suffix.replace("c.candidate_id", "candidate_id")
                        + " "
                        "ORDER BY created_at DESC LIMIT ?",
                        (query, query, query, *scope_params, limit),
                    ).fetchall()
            result = [self._candidate_dict(row, conn) for row in rows]
            if logical_project_id:
                for item in result:
                    item["logical_project_id"] = logical_project_id
                    item["logical_scope_raw_project_ids"] = logical_raw_ids
            return result

    def graph(
        self,
        *,
        project_id: str | None = None,
        logical_project_id: str | None = None,
        task_id: str | None = None,
        limit: int = 300,
        include_quarantine: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded graph projection from SQLite facts.

        Quarantined candidate observations are hidden by default while their
        canonical events remain visible as provenance.  Auditing clients can
        pass ``include_quarantine=True`` to inspect the complete projection.
        """

        limit = max(20, min(int(limit), 2000))
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        logical_raw_ids = raw_project_ids_for_logical(self.db, logical_project_id)

        def add_node(node_id: str, kind: str, label: str, **metadata: Any) -> None:
            if node_id not in nodes:
                nodes[node_id] = {"id": node_id, "kind": kind, "label": label, **metadata}

        def add_edge(source: str, target: str, relation: str, **metadata: Any) -> None:
            key = (source, target, relation)
            edges.setdefault(
                key, {"source": source, "target": target, "relation": relation, **metadata}
            )

        with self.db.connection() as conn:
            task_params_list: list[Any] = []
            task_clauses: list[str] = []
            if task_id:
                task_clauses.append("task_id=?")
                task_params_list.append(task_id)
            if project_id:
                task_clauses.append("project_id=?")
                task_params_list.append(project_id)
            if logical_project_id:
                if logical_raw_ids:
                    placeholders = ",".join("?" for _ in logical_raw_ids)
                    task_clauses.append(f"project_id IN ({placeholders})")
                    task_params_list.extend(logical_raw_ids)
                else:
                    task_clauses.append("1=0")
            task_params: tuple[Any, ...] = tuple(task_params_list)
            task_rows = conn.execute(
                "SELECT task_id,project_id,title,status FROM tasks "
                + (f"WHERE {' AND '.join(task_clauses)} " if task_clauses else "")
                + "ORDER BY updated_at DESC LIMIT 100",
                task_params,
            ).fetchall()
            selected_task_ids = [str(row["task_id"]) for row in task_rows]
            if not selected_task_ids:
                return {
                    "nodes": [],
                    "edges": [],
                    "truncated": False,
                    "task_id": task_id,
                    "project_id": project_id,
                    "logical_project_id": logical_project_id,
                }
            for row in task_rows:
                tid = str(row["task_id"])
                add_node(
                    f"task:{tid}",
                    "task",
                    str(row["title"] or tid),
                    task_id=tid,
                    project_id=str(row["project_id"]),
                    status=str(row["status"]),
                )
            placeholders = ",".join("?" for _ in selected_task_ids)
            event_rows = conn.execute(
                f"SELECT * FROM events WHERE task_id IN ({placeholders}) ORDER BY occurred_at,seq LIMIT ?",
                (*selected_task_ids, limit),
            ).fetchall()
            session_seen: set[str] = set()
            for row in event_rows:
                event_id = str(row["event_id"])
                tid = str(row["task_id"])
                sid = row["session_id"]
                event_node = f"event:{event_id}"
                payload = json_value(row["payload_json"], {})
                text = ""
                if isinstance(payload, dict):
                    text = str(
                        payload.get("text")
                        or payload.get("message")
                        or payload.get("summary")
                        or ""
                    )
                add_node(
                    event_node,
                    "event",
                    _trim_label(text or str(row["event_type"]), 90),
                    event_id=event_id,
                    event_type=str(row["event_type"]),
                    seq=int(row["seq"]),
                    occurred_at=str(row["occurred_at"]),
                    completeness=str(row["completeness"]),
                )
                quality = conn.execute(
                    "SELECT role,decision,signal_score,classifier_version FROM event_quality_evaluations WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if quality is not None:
                    nodes[event_node]["quality"] = {
                        "role": str(quality["role"]),
                        "decision": str(quality["decision"]),
                        "signal_score": float(quality["signal_score"]),
                        "classifier_version": str(quality["classifier_version"]),
                    }
                add_edge(f"task:{tid}", event_node, "contains")
                if sid:
                    sid = str(sid)
                    if sid not in session_seen:
                        session_seen.add(sid)
                        add_node(f"session:{sid}", "session", sid, session_id=sid)
                        add_edge(f"task:{tid}", f"session:{sid}", "has_session")
                    add_edge(f"session:{sid}", event_node, "records")
                for artifact in json_value(row["artifacts_json"], []):
                    if not isinstance(artifact, dict) or not artifact.get("path"):
                        continue
                    path = str(artifact["path"])
                    file_node = f"file:{path}"
                    add_node(file_node, "file", path, path=path)
                    add_edge(event_node, file_node, "mentions")

            candidate_query = (
                f"SELECT c.* FROM memory_candidates c WHERE c.task_id IN ({placeholders})"
            )
            if not include_quarantine:
                candidate_query += (
                    " AND NOT EXISTS (SELECT 1 FROM candidate_quality_reviews cq "
                    "WHERE cq.candidate_id=c.candidate_id AND cq.decision='quarantine')"
                )
            candidate_query += " ORDER BY c.created_at DESC LIMIT ?"
            candidate_rows = conn.execute(
                candidate_query,
                (*selected_task_ids, limit),
            ).fetchall()
            for row in candidate_rows:
                cid = str(row["candidate_id"])
                candidate_node = f"candidate:{cid}"
                add_node(
                    candidate_node,
                    "memory",
                    _trim_label(str(row["statement"]), 120),
                    candidate_id=cid,
                    candidate_kind=str(row["kind"]),
                    confidence=float(row["confidence"]),
                    lifecycle_hint=str(row["lifecycle_hint"]),
                    uncertainty=str(row["uncertainty"]),
                    aliases=json_value(row["aliases_json"], []),
                )
                quality = conn.execute(
                    "SELECT decision,quality_score,dimensions_json,reasons_json,classifier_version "
                    "FROM candidate_quality_reviews WHERE candidate_id=?",
                    (cid,),
                ).fetchone()
                if quality is not None:
                    nodes[candidate_node]["quality"] = {
                        "decision": str(quality["decision"]),
                        "quality_score": float(quality["quality_score"]),
                        "dimensions": json_value(quality["dimensions_json"], {}),
                        "reasons": json_value(quality["reasons_json"], []),
                        "classifier_version": str(quality["classifier_version"]),
                    }
                add_edge(f"task:{row['task_id']}", candidate_node, "proposes")
                links = conn.execute(
                    "SELECT target_type,target_id,relation_type,metadata_json FROM memory_candidate_links WHERE candidate_id=?",
                    (cid,),
                ).fetchall()
                for link in links:
                    target_type = str(link["target_type"])
                    target_id = str(link["target_id"])
                    relation = str(link["relation_type"])
                    if target_type == "event":
                        target_node = f"event:{target_id}"
                        if target_node in nodes:
                            add_edge(candidate_node, target_node, relation)
                    elif target_type == "file":
                        target_node = f"file:{target_id}"
                        add_node(target_node, "file", target_id, path=target_id)
                        add_edge(candidate_node, target_node, relation)
                    elif target_type == "symbol":
                        target_node = f"symbol:{target_id}"
                        add_node(target_node, "symbol", target_id, symbol=target_id)
                        add_edge(candidate_node, target_node, relation)
                    elif target_type == "task":
                        target_node = f"task:{target_id}"
                        if target_node in nodes:
                            add_edge(candidate_node, target_node, relation)
                    elif target_type == "candidate":
                        target_node = f"candidate:{target_id}"
                        if target_node in nodes:
                            add_edge(candidate_node, target_node, relation)
        return {
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
            "truncated": len(event_rows) >= limit if "event_rows" in locals() else False,
            "task_id": task_id,
            "project_id": project_id,
            "logical_project_id": logical_project_id,
        }

    @staticmethod
    def _available_candidate_id(conn: sqlite3.Connection, candidate: Candidate, run_id: str) -> str:
        base_id = candidate.candidate_id
        candidate_id = base_id
        suffix = run_id[:8]
        attempt = 0
        while conn.execute(
            "SELECT 1 FROM memory_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone():
            attempt += 1
            candidate_id = (
                f"{base_id}-{suffix}" if attempt == 1 else f"{base_id}-{suffix}-{attempt}"
            )
        return candidate_id

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": str(row["run_id"]),
            "project_id": str(row["project_id"]),
            "task_id": str(row["task_id"]),
            "session_id": row["session_id"],
            "input_hash": str(row["input_hash"]),
            "extractor_version": str(row["extractor_version"]),
            "prompt_version": str(row["prompt_version"]),
            "schema_version": str(row["schema_version"]),
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "status": str(row["status"]),
            "attempts": int(row["attempts"]),
            "error": row["error"],
            "result_hash": row["result_hash"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _candidate_dict(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        cid = str(row["candidate_id"])
        evidence = [
            str(item[0])
            for item in conn.execute(
                "SELECT event_id FROM memory_candidate_evidence WHERE candidate_id=? ORDER BY ordinal",
                (cid,),
            ).fetchall()
        ]
        result = {
            "candidate_id": cid,
            "run_id": str(row["run_id"]),
            "project_id": str(row["project_id"]),
            "task_id": str(row["task_id"]),
            "session_id": row["session_id"],
            "kind": str(row["kind"]),
            "statement": str(row["statement"]),
            "aliases": json_value(row["aliases_json"], []),
            "bindings": json_value(row["bindings_json"], []),
            "evidence_event_ids": evidence,
            "confidence": float(row["confidence"]),
            "uncertainty": str(row["uncertainty"]),
            "lifecycle_hint": str(row["lifecycle_hint"]),
            "relation_hints": json_value(row["relation_hints_json"], []),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        quality = conn.execute(
            "SELECT decision,quality_score,dimensions_json,reasons_json,classifier_version,reviewed_at "
            "FROM candidate_quality_reviews WHERE candidate_id=?",
            (cid,),
        ).fetchone()
        if quality is not None:
            result["quality"] = {
                "decision": str(quality["decision"]),
                "quality_score": float(quality["quality_score"]),
                "dimensions": json_value(quality["dimensions_json"], {}),
                "reasons": json_value(quality["reasons_json"], []),
                "classifier_version": str(quality["classifier_version"]),
                "reviewed_at": str(quality["reviewed_at"]),
            }
        else:
            result["quality"] = None
        return result


def _trim_label(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"
