"""SQLite persistence for quality evaluations, runs, and project aliases."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from ..storage.database import Database
from ..storage.repository import json_text
from .models import CandidateQualityReview, EventQualityEvaluation
from .project_scope import ScopeResolution, logical_project_id


_ALIAS_PRIORITY = {
    "explicit": 0,
    "repo": 1,
    "basename": 2,
    "root": 3,
    "inferred": 4,
}
_ALIAS_PRIORITY_SQL = (
    "CASE alias_type WHEN 'explicit' THEN 0 WHEN 'repo' THEN 1 "
    "WHEN 'basename' THEN 2 WHEN 'root' THEN 3 ELSE 4 END"
)
QUALITY_WRITE_BATCH_SIZE = 500


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


class QualityStore:
    """Transactional read/write facade for the Phase 4A projections."""

    def __init__(self, database: Database):
        self.db = database
        self.db.ensure_initialized()

    @staticmethod
    def _event_quality_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": str(row["event_id"]),
            "project_id": str(row["project_id"]),
            "task_id": str(row["task_id"]),
            "role": str(row["role"]),
            "decision": str(row["decision"]),
            "signal_score": float(row["signal_score"]),
            "dimensions": _safe_json(row["dimensions_json"], {}),
            "reasons": _safe_json(row["reasons_json"], []),
            "classifier_version": str(row["classifier_version"]),
            "evaluated_at": str(row["evaluated_at"]),
        }

    @staticmethod
    def _candidate_quality_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "candidate_id": str(row["candidate_id"]),
            "project_id": str(row["project_id"]),
            "task_id": str(row["task_id"]),
            "decision": str(row["decision"]),
            "quality_score": float(row["quality_score"]),
            "dimensions": _safe_json(row["dimensions_json"], {}),
            "reasons": _safe_json(row["reasons_json"], []),
            "classifier_version": str(row["classifier_version"]),
            "reviewed_at": str(row["reviewed_at"]),
        }

    def upsert_event_evaluation(self, evaluation: EventQualityEvaluation) -> dict[str, Any]:
        evaluated_at = evaluation.evaluated_at or _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO event_quality_evaluations(event_id,project_id,task_id,role,decision,signal_score,"
                "dimensions_json,reasons_json,classifier_version,evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO UPDATE SET project_id=excluded.project_id,task_id=excluded.task_id,"
                "role=excluded.role,decision=excluded.decision,signal_score=excluded.signal_score,"
                "dimensions_json=excluded.dimensions_json,reasons_json=excluded.reasons_json,"
                "classifier_version=excluded.classifier_version,evaluated_at=excluded.evaluated_at",
                (
                    evaluation.event_id,
                    evaluation.project_id,
                    evaluation.task_id,
                    evaluation.role,
                    evaluation.decision,
                    float(evaluation.signal_score),
                    json_text(evaluation.dimensions),
                    json_text(list(evaluation.reasons)),
                    evaluation.classifier_version,
                    evaluated_at,
                ),
            )
        return {**evaluation.as_dict(), "evaluated_at": evaluated_at}

    def upsert_event_evaluations(self, evaluations: Iterable[EventQualityEvaluation]) -> int:
        rows = list(evaluations)
        if not rows:
            return 0
        now = _now()
        for offset in range(0, len(rows), QUALITY_WRITE_BATCH_SIZE):
            batch = rows[offset : offset + QUALITY_WRITE_BATCH_SIZE]
            with self.db.transaction() as conn:
                for evaluation in batch:
                    conn.execute(
                        "INSERT INTO event_quality_evaluations(event_id,project_id,task_id,role,decision,signal_score,"
                        "dimensions_json,reasons_json,classifier_version,evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(event_id) DO UPDATE SET project_id=excluded.project_id,task_id=excluded.task_id,"
                        "role=excluded.role,decision=excluded.decision,signal_score=excluded.signal_score,"
                        "dimensions_json=excluded.dimensions_json,reasons_json=excluded.reasons_json,"
                        "classifier_version=excluded.classifier_version,evaluated_at=excluded.evaluated_at",
                        (
                            evaluation.event_id,
                            evaluation.project_id,
                            evaluation.task_id,
                            evaluation.role,
                            evaluation.decision,
                            float(evaluation.signal_score),
                            json_text(evaluation.dimensions),
                            json_text(list(evaluation.reasons)),
                            evaluation.classifier_version,
                            evaluation.evaluated_at or now,
                        ),
                    )
        return len(rows)

    def get_event_evaluation(self, event_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM event_quality_evaluations WHERE event_id=?", (event_id,)
            ).fetchone()
            return self._event_quality_from_row(row) if row else None

    def get_event_evaluations(self, event_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = sorted({str(value) for value in event_ids if str(value)})
        if not ids:
            return {}
        with self.db.connection() as conn:
            result: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(ids), QUALITY_WRITE_BATCH_SIZE):
                chunk = ids[offset : offset + QUALITY_WRITE_BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM event_quality_evaluations WHERE event_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                result.update({str(row["event_id"]): self._event_quality_from_row(row) for row in rows})
            return result

    def upsert_candidate_review(self, review: CandidateQualityReview) -> dict[str, Any]:
        reviewed_at = review.reviewed_at or _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO candidate_quality_reviews(candidate_id,project_id,task_id,decision,quality_score,"
                "dimensions_json,reasons_json,classifier_version,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET project_id=excluded.project_id,task_id=excluded.task_id,"
                "decision=excluded.decision,quality_score=excluded.quality_score,dimensions_json=excluded.dimensions_json,"
                "reasons_json=excluded.reasons_json,classifier_version=excluded.classifier_version,reviewed_at=excluded.reviewed_at",
                (
                    review.candidate_id,
                    review.project_id,
                    review.task_id,
                    review.decision,
                    float(review.quality_score),
                    json_text(review.dimensions),
                    json_text(list(review.reasons)),
                    review.classifier_version,
                    reviewed_at,
                ),
            )
        return {**review.as_dict(), "reviewed_at": reviewed_at}

    def upsert_candidate_reviews(self, reviews: Iterable[CandidateQualityReview]) -> int:
        rows = list(reviews)
        if not rows:
            return 0
        now = _now()
        for offset in range(0, len(rows), QUALITY_WRITE_BATCH_SIZE):
            batch = rows[offset : offset + QUALITY_WRITE_BATCH_SIZE]
            with self.db.transaction() as conn:
                for review in batch:
                    conn.execute(
                        "INSERT INTO candidate_quality_reviews(candidate_id,project_id,task_id,decision,quality_score,"
                        "dimensions_json,reasons_json,classifier_version,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(candidate_id) DO UPDATE SET project_id=excluded.project_id,task_id=excluded.task_id,"
                        "decision=excluded.decision,quality_score=excluded.quality_score,dimensions_json=excluded.dimensions_json,"
                        "reasons_json=excluded.reasons_json,classifier_version=excluded.classifier_version,reviewed_at=excluded.reviewed_at",
                        (
                            review.candidate_id,
                            review.project_id,
                            review.task_id,
                            review.decision,
                            float(review.quality_score),
                            json_text(review.dimensions),
                            json_text(list(review.reasons)),
                            review.classifier_version,
                            review.reviewed_at or now,
                        ),
                    )
        return len(rows)

    def get_candidate_review(self, candidate_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM candidate_quality_reviews WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            return self._candidate_quality_from_row(row) if row else None

    def list_event_evaluations(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        decision: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if decision:
            clauses.append("decision=?")
            params.append(decision)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 100_000))
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM event_quality_evaluations {where} ORDER BY evaluated_at,event_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._event_quality_from_row(row) for row in rows]

    def list_candidate_reviews(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        decision: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if decision:
            clauses.append("decision=?")
            params.append(decision)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 100_000))
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM candidate_quality_reviews {where} ORDER BY reviewed_at,candidate_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._candidate_quality_from_row(row) for row in rows]

    def start_run(
        self,
        *,
        scope: str,
        input_hash: str,
        classifier_version: str,
        mode: str,
        project_id: str | None = None,
        task_id: str | None = None,
        logical_project_id_value: str | None = None,
    ) -> str:
        # A CLI/API process can be interrupted while a replay is running.  In
        # that case the audit row must not remain ``running`` forever: the
        # next invocation repairs only rows old enough to be considered stale
        # and leaves an explicit failure reason for operators.  This is an
        # additive audit repair; canonical events/candidates are untouched.
        self.recover_stale_runs()
        run_id = f"quality-{uuid.uuid4()}"
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO quality_runs(run_id,scope,project_id,task_id,logical_project_id,input_hash,"
                "classifier_version,mode,status,counts_json,started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    scope,
                    project_id,
                    task_id,
                    logical_project_id_value,
                    input_hash,
                    classifier_version,
                    mode,
                    "running",
                    "{}",
                    _now(),
                ),
            )
        return run_id

    def recover_stale_runs(self, *, max_age_seconds: int = 300) -> int:
        """Mark interrupted quality runs as failed after a bounded grace period.

        ``quality_runs`` is an audit projection, so an orphaned ``running``
        row is misleading but harmless to source data.  Recovery is
        deliberately conservative: only rows older than five minutes by
        default are changed, which protects a legitimately long replay.
        """

        age = max(1, int(max_age_seconds))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=age)).replace(
            microsecond=0
        ).isoformat()
        finished = _now()
        with self.db.transaction() as conn:
            result = conn.execute(
                "UPDATE quality_runs SET status='failed',"
                "error=COALESCE(error,?),finished_at=? "
                "WHERE status='running' AND started_at<?",
                (f"interrupted: stale running quality run recovered after {age}s", finished, cutoff),
            )
            return int(result.rowcount)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        counts: Mapping[str, Any],
        error: str | None = None,
    ) -> bool:
        with self.db.transaction() as conn:
            return (
                conn.execute(
                    "UPDATE quality_runs SET status=?,counts_json=?,error=?,finished_at=? WHERE run_id=?",
                    (status, json_text(dict(counts)), error[:4000] if error else None, _now(), run_id),
                ).rowcount
                == 1
            )

    def latest_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM quality_runs ORDER BY started_at DESC,run_id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                {
                    "run_id": str(row["run_id"]),
                    "scope": str(row["scope"]),
                    "project_id": row["project_id"],
                    "task_id": row["task_id"],
                    "logical_project_id": row["logical_project_id"],
                    "input_hash": str(row["input_hash"]),
                    "classifier_version": str(row["classifier_version"]),
                    "mode": str(row["mode"]),
                    "status": str(row["status"]),
                    "counts": _safe_json(row["counts_json"], {}),
                    "error": row["error"],
                    "started_at": str(row["started_at"]),
                    "finished_at": row["finished_at"],
                }
                for row in rows
            ]

    def ensure_logical_project(
        self,
        *,
        raw_project_id: str,
        identity_key: str,
        display_name: str,
        normalized_root: str = "",
        alias_type: str = "root",
        alias_value: str = "",
        confidence: float = 1.0,
        evidence: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ScopeResolution:
        logical_id = logical_project_id(identity_key)
        now = _now()
        alias_value = alias_value or normalized_root or raw_project_id
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO logical_projects(logical_project_id,display_name,identity_key,status,metadata_json,created_at,updated_at) "
                "VALUES (?,?,?,'active',?,?,?) ON CONFLICT(identity_key) DO UPDATE SET display_name=excluded.display_name,"
                "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                (
                    logical_id,
                    display_name or raw_project_id,
                    identity_key,
                    json_text(dict(metadata or {})),
                    now,
                    now,
                ),
            )
            existing = conn.execute(
                "SELECT logical_project_id,alias_type,confidence FROM project_aliases "
                "WHERE raw_project_id=? ORDER BY "
                + _ALIAS_PRIORITY_SQL
                + ",confidence DESC,updated_at DESC,alias_id LIMIT 1",
                (raw_project_id,),
            ).fetchone()
            # A reviewed/stronger mapping wins over a later heuristic.  The
            # new root is still retained as evidence on the effective logical
            # project instead of silently changing project ownership.
            existing_is_stronger = bool(
                existing
                and _ALIAS_PRIORITY.get(str(existing["alias_type"]), 99)
                < _ALIAS_PRIORITY.get(alias_type, 99)
            )
            if existing and str(existing["logical_project_id"]) != logical_id and (
                existing_is_stronger or confidence < 0.95
            ):
                logical_id = str(existing["logical_project_id"])
            conn.execute(
                "INSERT INTO project_aliases(alias_id,raw_project_id,logical_project_id,alias_type,alias_value,normalized_root,"
                "confidence,evidence_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(raw_project_id,logical_project_id,alias_type,alias_value) DO UPDATE SET normalized_root=excluded.normalized_root,"
                "confidence=MAX(project_aliases.confidence,excluded.confidence),evidence_json=excluded.evidence_json,updated_at=excluded.updated_at",
                (
                    f"alias-{uuid.uuid4()}",
                    raw_project_id,
                    logical_id,
                    alias_type,
                    alias_value,
                    normalized_root or None,
                    max(0.0, min(1.0, float(confidence))),
                    json_text(dict(evidence or {})),
                    now,
                    now,
                ),
            )
            project = conn.execute(
                "SELECT identity_key,display_name FROM logical_projects WHERE logical_project_id=?",
                (logical_id,),
            ).fetchone()
            identity = str(project["identity_key"]) if project else identity_key
            name = str(project["display_name"]) if project else display_name
        return ScopeResolution(
            raw_project_id=raw_project_id,
            logical_project_id=logical_id,
            identity_key=identity,
            display_name=name,
            normalized_root=normalized_root,
            confidence=float(confidence),
            alias_type=alias_type,
            inferred=alias_type in {"basename", "inferred"},
        )

    def register_alias(
        self,
        *,
        raw_project_id: str,
        logical_project_id_value: str,
        alias_value: str,
        alias_type: str = "explicit",
        normalized_root: str = "",
        confidence: float = 1.0,
        evidence: Mapping[str, Any] | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Attach a reviewed raw id to an existing or named logical scope."""

        if alias_type not in {"root", "repo", "basename", "explicit", "inferred"}:
            raise ValueError(f"unsupported project alias type: {alias_type}")
        now = _now()
        logical_id = str(logical_project_id_value).strip()
        raw_id = str(raw_project_id).strip()
        if not logical_id:
            raise ValueError("logical_project_id cannot be empty")
        if not raw_id:
            raise ValueError("raw_project_id cannot be empty")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE project_id=?", (raw_id,)).fetchone() is None:
                raise ValueError(f"raw project does not exist: {raw_id}")
            project = conn.execute(
                "SELECT logical_project_id,display_name FROM logical_projects WHERE logical_project_id=?",
                (logical_id,),
            ).fetchone()
            if project is None:
                name = display_name or logical_id
                conn.execute(
                    "INSERT INTO logical_projects(logical_project_id,display_name,identity_key,status,metadata_json,created_at,updated_at) "
                    "VALUES (?,?,?,'active','{}',?,?)",
                    (logical_id, name, f"explicit:{logical_id}", now, now),
                )
            conn.execute(
                "INSERT INTO project_aliases(alias_id,raw_project_id,logical_project_id,alias_type,alias_value,normalized_root,"
                "confidence,evidence_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(raw_project_id,logical_project_id,alias_type,alias_value) DO UPDATE SET normalized_root=excluded.normalized_root,"
                "confidence=MAX(project_aliases.confidence,excluded.confidence),evidence_json=excluded.evidence_json,updated_at=excluded.updated_at",
                (
                    f"alias-{uuid.uuid4()}",
                    raw_id,
                    logical_id,
                    alias_type,
                    alias_value,
                    normalized_root or None,
                    max(0.0, min(1.0, float(confidence))),
                    json_text(dict(evidence or {})),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM project_aliases WHERE raw_project_id=? AND logical_project_id=? "
                "AND alias_type=? AND alias_value=?",
                (raw_id, logical_id, alias_type, alias_value),
            ).fetchone()
            if row is None:
                raise RuntimeError("project alias was not persisted")
            return self._alias_from_row(row)

    def list_logical_projects(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 10_000))
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM logical_projects ORDER BY updated_at DESC,logical_project_id LIMIT ?", (limit,)
            ).fetchall()
            alias_rows = conn.execute(
                "SELECT * FROM project_aliases ORDER BY raw_project_id,"
                + _ALIAS_PRIORITY_SQL
                + ",confidence DESC,updated_at DESC,alias_id"
            ).fetchall()
            effective_by_raw: dict[str, str] = {}
            aliases_by_logical: dict[str, list[sqlite3.Row]] = {}
            for alias in alias_rows:
                raw_id = str(alias["raw_project_id"])
                effective_by_raw.setdefault(raw_id, str(alias["alias_id"]))
                aliases_by_logical.setdefault(str(alias["logical_project_id"]), []).append(alias)
            result: list[dict[str, Any]] = []
            for row in rows:
                aliases = aliases_by_logical.get(str(row["logical_project_id"]), [])
                result.append(
                    {
                        "logical_project_id": str(row["logical_project_id"]),
                        "display_name": str(row["display_name"]),
                        "identity_key": str(row["identity_key"]),
                        "status": str(row["status"]),
                        "metadata": _safe_json(row["metadata_json"], {}),
                        "created_at": str(row["created_at"]),
                        "updated_at": str(row["updated_at"]),
                        "aliases": [
                            self._alias_from_row(
                                alias,
                                effective=(
                                    effective_by_raw.get(str(alias["raw_project_id"]))
                                    == str(alias["alias_id"])
                                ),
                            )
                            for alias in aliases
                        ],
                    }
                )
            return result

    @staticmethod
    def _alias_from_row(
        row: sqlite3.Row, *, effective: bool | None = None
    ) -> dict[str, Any]:
        result = {
            "alias_id": str(row["alias_id"]),
            "raw_project_id": str(row["raw_project_id"]),
            "logical_project_id": str(row["logical_project_id"]),
            "alias_type": str(row["alias_type"]),
            "alias_value": str(row["alias_value"]),
            "normalized_root": row["normalized_root"],
            "confidence": float(row["confidence"]),
            "evidence": _safe_json(row["evidence_json"], {}),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        if effective is not None:
            result["effective"] = effective
        return result

    def aliases_for_raw_project(self, raw_project_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM project_aliases WHERE raw_project_id=? ORDER BY "
                + _ALIAS_PRIORITY_SQL
                + ",confidence DESC,updated_at DESC,alias_id",
                (raw_project_id,),
            ).fetchall()
            return [
                self._alias_from_row(row, effective=index == 0)
                for index, row in enumerate(rows)
            ]

    def raw_project_ids_for_logical(self, logical_project_id_value: str) -> list[str]:
        """Return only raw projects whose strongest alias targets this scope."""

        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT raw_project_id,logical_project_id FROM project_aliases ORDER BY raw_project_id,"
                + _ALIAS_PRIORITY_SQL
                + ",confidence DESC,updated_at DESC,alias_id"
            ).fetchall()
        effective: dict[str, str] = {}
        for row in rows:
            effective.setdefault(str(row["raw_project_id"]), str(row["logical_project_id"]))
        return sorted(
            raw_id
            for raw_id, logical_id in effective.items()
            if logical_id == logical_project_id_value
        )

    def logical_project_for_raw(self, raw_project_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT p.* FROM project_aliases a JOIN logical_projects p ON p.logical_project_id=a.logical_project_id "
                "WHERE a.raw_project_id=? ORDER BY "
                + _ALIAS_PRIORITY_SQL
                + ",a.confidence DESC,a.updated_at DESC,a.alias_id LIMIT 1",
                (raw_project_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "logical_project_id": str(row["logical_project_id"]),
                "display_name": str(row["display_name"]),
                "identity_key": str(row["identity_key"]),
                "status": str(row["status"]),
            }

    def report(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        logical_project_id_value: str | None = None,
        classifier_version: str | None = None,
    ) -> dict[str, Any]:
        effective_raw_ids = (
            self.raw_project_ids_for_logical(logical_project_id_value)
            if logical_project_id_value
            else []
        )

        def clauses_for(alias: str) -> tuple[str, list[Any]]:
            clauses: list[str] = []
            values: list[Any] = []
            if project_id:
                clauses.append(f"{alias}.project_id=?")
                values.append(project_id)
            if task_id:
                clauses.append(f"{alias}.task_id=?")
                values.append(task_id)
            if logical_project_id_value:
                if effective_raw_ids:
                    placeholders = ",".join("?" for _ in effective_raw_ids)
                    clauses.append(f"{alias}.project_id IN ({placeholders})")
                    values.extend(effective_raw_ids)
                else:
                    clauses.append("1=0")
            return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), values

        event_where, event_params = clauses_for("e")
        candidate_where, candidate_params = clauses_for("c")
        with self.db.connection() as conn:
            event_total = int(
                conn.execute(f"SELECT COUNT(*) FROM events e {event_where}", tuple(event_params)).fetchone()[0]
            )
            candidate_total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM memory_candidates c {candidate_where}", tuple(candidate_params)
                ).fetchone()[0]
            )
            event_quality_where, event_quality_params = clauses_for("q")
            if classifier_version:
                event_quality_where = (
                    event_quality_where + " AND " if event_quality_where else "WHERE "
                ) + "q.classifier_version=?"
                event_quality_params.append(classifier_version)
            event_rows = conn.execute(
                "SELECT q.decision,COUNT(*) AS n FROM event_quality_evaluations q "
                + event_quality_where
                + " GROUP BY q.decision",
                tuple(event_quality_params),
            ).fetchall()
            candidate_quality_where, candidate_quality_params = clauses_for("c")
            if classifier_version:
                candidate_quality_where = (
                    candidate_quality_where + " AND " if candidate_quality_where else "WHERE "
                ) + "q.classifier_version=?"
                candidate_quality_params.append(classifier_version)
            candidate_rows = conn.execute(
                "SELECT q.decision,COUNT(*) AS n FROM candidate_quality_reviews q "
                "JOIN memory_candidates c ON c.candidate_id=q.candidate_id "
                + candidate_quality_where
                + " GROUP BY q.decision",
                tuple(candidate_quality_params),
            ).fetchall()
            evaluated_events = int(
                conn.execute(
                    "SELECT COUNT(*) FROM event_quality_evaluations q "
                    + event_quality_where,
                    tuple(event_quality_params),
                ).fetchone()[0]
            )
            reviewed_candidates = int(
                conn.execute(
                    "SELECT COUNT(*) FROM candidate_quality_reviews q JOIN memory_candidates c ON c.candidate_id=q.candidate_id "
                    + candidate_quality_where,
                    tuple(candidate_quality_params),
                ).fetchone()[0]
            )
            event_scope_mismatches = int(
                conn.execute(
                    "SELECT COUNT(*) FROM event_quality_evaluations q "
                    + event_quality_where
                    + " AND instr(q.dimensions_json, '\"scope_mismatch\":true') > 0"
                    if event_quality_where
                    else "SELECT COUNT(*) FROM event_quality_evaluations q "
                    "WHERE instr(q.dimensions_json, '\"scope_mismatch\":true') > 0",
                    tuple(event_quality_params),
                ).fetchone()[0]
            )
            candidate_scope_mismatches = int(
                conn.execute(
                    "SELECT COUNT(*) FROM candidate_quality_reviews q "
                    "JOIN memory_candidates c ON c.candidate_id=q.candidate_id "
                    + candidate_quality_where
                    + " AND instr(q.dimensions_json, '\"scope_mismatch\":true') > 0"
                    if candidate_quality_where
                    else "SELECT COUNT(*) FROM candidate_quality_reviews q "
                    "JOIN memory_candidates c ON c.candidate_id=q.candidate_id "
                    "WHERE instr(q.dimensions_json, '\"scope_mismatch\":true') > 0",
                    tuple(candidate_quality_params),
                ).fetchone()[0]
            )
            aliases = int(
                conn.execute(
                    "SELECT COUNT(*) FROM project_aliases"
                    + (
                        " WHERE raw_project_id=?"
                        if project_id
                        else " WHERE logical_project_id=?"
                        if logical_project_id_value
                        else ""
                    ),
                    (project_id or logical_project_id_value,) if (project_id or logical_project_id_value) else (),
                ).fetchone()[0]
            )
            effective_alias_rows = conn.execute(
                "SELECT raw_project_id,logical_project_id FROM ("
                "SELECT raw_project_id,logical_project_id,ROW_NUMBER() OVER ("
                "PARTITION BY raw_project_id ORDER BY "
                + _ALIAS_PRIORITY_SQL
                + ",confidence DESC,updated_at DESC,alias_id"
                ") AS rn FROM project_aliases) WHERE rn=1"
            ).fetchall()
            if logical_project_id_value:
                effective_raw_project_count = sum(
                    str(row["logical_project_id"]) == logical_project_id_value
                    for row in effective_alias_rows
                )
                logical_project_count = 1 if effective_raw_project_count else 0
            else:
                effective_raw_project_count = len(effective_alias_rows)
                logical_project_count = len(
                    {str(row["logical_project_id"]) for row in effective_alias_rows}
                )
        return {
            "filters": {
                "project_id": project_id,
                "task_id": task_id,
                "logical_project_id": logical_project_id_value,
            },
            "events": {
                "total": event_total,
                "evaluated": evaluated_events,
                "unreviewed": max(0, event_total - evaluated_events),
                "scope_mismatches": event_scope_mismatches,
                "decisions": {str(row["decision"]): int(row["n"]) for row in event_rows},
            },
            "candidates": {
                "total": candidate_total,
                "reviewed": reviewed_candidates,
                "unreviewed": max(0, candidate_total - reviewed_candidates),
                "scope_mismatches": candidate_scope_mismatches,
                "decisions": {str(row["decision"]): int(row["n"]) for row in candidate_rows},
            },
            "alias_count": aliases,
            "effective_raw_project_count": effective_raw_project_count,
            "logical_project_count": logical_project_count,
        }
