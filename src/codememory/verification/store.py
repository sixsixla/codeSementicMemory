"""SQLite persistence for code snapshots and binding-verification audits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..storage.database import Database
from ..storage.repository import json_text
from .models import SnapshotManifest


WRITE_BATCH_SIZE = 500


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _value(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return default
    return value


def snapshot_id(logical_project_id: str, manifest: SnapshotManifest) -> str:
    digest = hashlib.sha256(
        f"{logical_project_id}\n{manifest.provider}\n{manifest.manifest_hash}".encode("utf-8")
    ).hexdigest()[:32]
    return f"snapshot-{digest}"


class VerificationStore:
    """Transactional store for immutable snapshots and current projections."""

    def __init__(self, database: Database):
        self.db = database
        self.db.ensure_initialized()

    def ensure_snapshot(self, logical_project_id: str, manifest: SnapshotManifest) -> dict[str, Any]:
        sid = snapshot_id(logical_project_id, manifest)
        manifest_json = json_text(manifest.canonical_dict())
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO code_snapshots(snapshot_id,logical_project_id,provider,source_ref,root_path,revision,manifest_hash,status,metadata_json,captured_at,manifest_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(snapshot_id) DO UPDATE SET "
                "manifest_json=CASE WHEN code_snapshots.manifest_json='{}' THEN excluded.manifest_json ELSE code_snapshots.manifest_json END",
                (
                    sid,
                    logical_project_id,
                    manifest.provider,
                    manifest.source_ref,
                    manifest.root_path,
                    manifest.revision,
                    manifest.manifest_hash,
                    manifest.status,
                    json_text({
                        **manifest.metadata,
                        "schema_version": "codememory.verification_manifest.v1",
                        "file_count": manifest.file_count,
                        "symbol_count": manifest.symbol_count,
                    }),
                    manifest.captured_at,
                    manifest_json,
                ),
            )
            row = conn.execute("SELECT * FROM code_snapshots WHERE snapshot_id=?", (sid,)).fetchone()
        if row is None:  # pragma: no cover - guarded by INSERT/SELECT in one transaction
            raise RuntimeError("snapshot was not persisted")
        return self._snapshot_from_row(row)

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row, *, include_manifest: bool = False) -> dict[str, Any]:
        value = {
            "snapshot_id": str(row["snapshot_id"]),
            "logical_project_id": str(row["logical_project_id"]),
            "provider": str(row["provider"]),
            "source_ref": row["source_ref"],
            "root_path": row["root_path"],
            "revision": row["revision"],
            "manifest_hash": str(row["manifest_hash"]),
            "status": str(row["status"]),
            "metadata": _value(row["metadata_json"], {}),
            "captured_at": str(row["captured_at"]),
        }
        # The exact normalized manifest is retained for audit/replay.  Keep it
        # out of aggregate reports by default; complete-repository manifests
        # can be large.  Snapshot detail callers can opt in explicitly.
        if include_manifest:
            value["manifest"] = _value(row["manifest_json"], {})
        return value

    def snapshots_for_project(
        self,
        logical_project_id: str,
        *,
        limit: int = 100,
        include_manifest: bool = False,
    ) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM code_snapshots WHERE logical_project_id=? "
                "ORDER BY julianday(captured_at) DESC,captured_at DESC,snapshot_id DESC LIMIT ?",
                (logical_project_id, max(1, min(int(limit), 1000))),
            ).fetchall()
            return [self._snapshot_from_row(row, include_manifest=include_manifest) for row in rows]

    def get_snapshot(self, snapshot_id_value: str, *, include_manifest: bool = True) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM code_snapshots WHERE snapshot_id=?",
                (str(snapshot_id_value),),
            ).fetchone()
        return self._snapshot_from_row(row, include_manifest=include_manifest) if row else None

    def list_current_bindings(
        self,
        raw_project_ids: Sequence[str],
        *,
        task_id: str | None = None,
        task_ids: Sequence[str] = (),
        limit: int = 100_000,
        include_quarantine: bool = False,
    ) -> list[dict[str, Any]]:
        ids = sorted({str(item) for item in raw_project_ids if str(item)})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        filters = [f"c.project_id IN ({placeholders})"]
        filter_params: list[Any] = list(ids)
        selected_tasks = sorted({str(item) for item in task_ids if str(item)})
        if task_id:
            selected_tasks.append(str(task_id))
        selected_tasks = sorted(set(selected_tasks))
        if selected_tasks:
            task_placeholders = ",".join("?" for _ in selected_tasks)
            filters.append(f"c.task_id IN ({task_placeholders})")
            filter_params.extend(selected_tasks)
        quality = "" if include_quarantine else (
            " AND NOT EXISTS (SELECT 1 FROM memory_card_evidence qce "
            "JOIN memory_card_versions qcv ON qcv.card_version_id=qce.card_version_id "
            "JOIN candidate_quality_reviews qcr ON qcr.candidate_id=qce.candidate_id "
            "WHERE qcv.card_id=c.card_id AND qcr.decision='quarantine')"
        )
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT b.*,v.card_id,v.version_no,v.statement,c.project_id,c.task_id,c.kind AS card_kind,c.status AS card_status "
                "FROM memory_card_bindings b "
                "JOIN memory_card_versions v ON v.card_version_id=b.card_version_id "
                "JOIN memory_cards c ON c.card_id=v.card_id AND c.current_version_id=v.card_version_id "
                f"WHERE {' AND '.join(filters)} AND c.status NOT IN ('superseded','rejected') {quality} "
                "ORDER BY c.updated_at DESC,b.binding_id LIMIT ?",
                (*filter_params, max(1, min(int(limit), 100_000))),
            ).fetchall()
        return [self._binding_from_row(row) for row in rows]

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "binding_id": str(row["binding_id"]),
            "card_id": str(row["card_id"]),
            "card_version_id": str(row["card_version_id"]),
            "version_no": int(row["version_no"]),
            "statement": str(row["statement"]),
            "project_id": str(row["project_id"]),
            "task_id": row["task_id"],
            "card_kind": str(row["card_kind"]),
            "card_status": str(row["card_status"]),
            "role": str(row["role"]),
            "path": row["path"],
            "symbol": row["symbol"],
            "qualified_symbol": row["qualified_symbol"],
            "normalized_target": str(row["normalized_target"]),
            "status": str(row["status"]),
            "snapshot_id": row["snapshot_id"],
            "evidence_event_ids": _value(row["evidence_event_ids_json"], []),
            "metadata": _value(row["metadata_json"], {}),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def start_run(
        self,
        *,
        logical_project_id: str,
        input_hash: str,
        mode: str,
        provider_names: Iterable[str],
        snapshot_ids: Iterable[str] = (),
    ) -> str:
        if mode not in {"dry_run", "write"}:
            raise ValueError(f"unsupported verification mode: {mode}")
        self.recover_stale_runs()
        run_id = f"verification-{uuid.uuid4()}"
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO verification_runs(run_id,logical_project_id,mode,input_hash,provider_names_json,snapshot_ids_json,status,counts_json,started_at) "
                "VALUES (?,?,?,?,?,?, 'running','{}',?)",
                (
                    run_id,
                    logical_project_id,
                    mode,
                    input_hash,
                    json_text(sorted({str(item) for item in provider_names if str(item)})),
                    json_text(list(dict.fromkeys(str(item) for item in snapshot_ids if str(item)))),
                    _now(),
                ),
            )
        return run_id

    def set_run_snapshots(self, run_id: str, snapshot_ids: Iterable[str]) -> bool:
        with self.db.transaction() as conn:
            return (
                conn.execute(
                    "UPDATE verification_runs SET snapshot_ids_json=? WHERE run_id=?",
                    (json_text(list(dict.fromkeys(str(item) for item in snapshot_ids if str(item)))), run_id),
                ).rowcount
                == 1
            )

    def recover_stale_runs(self, *, max_age_seconds: int = 300) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1, int(max_age_seconds)))
        ).replace(microsecond=0).isoformat()
        with self.db.transaction() as conn:
            result = conn.execute(
                "UPDATE verification_runs SET status='failed',error=COALESCE(error,?),finished_at=? "
                "WHERE status='running' AND started_at<?",
                (
                    f"interrupted: stale verification run recovered after {max(1, int(max_age_seconds))}s",
                    _now(),
                    cutoff,
                ),
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
        if status not in {"succeeded", "failed", "not_applicable"}:
            raise ValueError(f"unsupported verification run status: {status}")
        with self.db.transaction() as conn:
            return (
                conn.execute(
                    "UPDATE verification_runs SET status=?,counts_json=?,error=?,finished_at=? WHERE run_id=?",
                    (status, json_text(dict(counts)), error[:4000] if error else None, _now(), run_id),
                ).rowcount
                == 1
            )

    def record_results(
        self,
        *,
        run_id: str,
        snapshot_id_value: str,
        bindings: Sequence[Mapping[str, Any]],
        results: Sequence[Mapping[str, Any]],
        apply_status: bool,
    ) -> dict[str, int]:
        by_id = {str(item.get("binding_id")): item for item in bindings}
        counters = Counter()
        for offset in range(0, len(results), WRITE_BATCH_SIZE):
            batch = results[offset : offset + WRITE_BATCH_SIZE]
            with self.db.transaction() as conn:
                for item in batch:
                    binding_id = str(item.get("binding_id") or "")
                    if not binding_id or binding_id not in by_id:
                        continue
                    status = str(item.get("status") or "unverified")
                    score = max(0.0, min(1.0, float(item.get("score") or 0.0)))
                    reasons = [str(value) for value in (item.get("reasons") or []) if str(value)]
                    evidence = dict(item.get("evidence") or {})
                    checked_at = str(item.get("checked_at") or _now())
                    current = by_id[binding_id]
                    old_status = str(current.get("status") or "unverified")
                    applied = bool(apply_status and (status != "unverified" or old_status == "unverified"))
                    verification_id = f"binding-verification-{run_id}-{binding_id}"
                    conn.execute(
                        "INSERT INTO binding_verifications(verification_id,run_id,binding_id,snapshot_id,status,score,resolved_path,resolved_symbol,evidence_json,reasons_json,applied,checked_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(run_id,binding_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,status=excluded.status,score=excluded.score,resolved_path=excluded.resolved_path,resolved_symbol=excluded.resolved_symbol,evidence_json=excluded.evidence_json,reasons_json=excluded.reasons_json,applied=excluded.applied,checked_at=excluded.checked_at",
                        (
                            verification_id,
                            run_id,
                            binding_id,
                            snapshot_id_value,
                            status,
                            score,
                            item.get("resolved_path"),
                            item.get("resolved_symbol"),
                            json_text(evidence),
                            json_text(reasons),
                            int(applied),
                            checked_at,
                        ),
                    )
                    metadata = dict(current.get("metadata") or {})
                    metadata["verification"] = {
                        "status": status,
                        "score": score,
                        "run_id": run_id,
                        "snapshot_id": snapshot_id_value,
                        "resolved_path": item.get("resolved_path"),
                        "resolved_symbol": item.get("resolved_symbol"),
                        "reasons": reasons[:20],
                        "checked_at": checked_at,
                        "applied": applied,
                    }
                    # Keep a bounded trail useful to the UI without turning a
                    # frequently checked binding into an unbounded JSON blob.
                    history = list(metadata.get("verification_history") or [])
                    history.append({"run_id": run_id, "status": status, "score": score, "checked_at": checked_at})
                    metadata["verification_history"] = history[-8:]
                    if applied:
                        conn.execute(
                            "UPDATE memory_card_bindings SET status=?,snapshot_id=?,metadata_json=?,updated_at=? WHERE binding_id=?",
                            (status, snapshot_id_value, json_text(metadata), checked_at, binding_id),
                        )
                    counters["applied" if applied else "skipped"] += 1
                    counters[status] += 1
        return dict(counters)

    def latest_runs(self, *, logical_project_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if logical_project_id:
            clauses.append("logical_project_id=?")
            params.append(logical_project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM verification_runs {where} ORDER BY started_at DESC,run_id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": str(row["run_id"]),
            "logical_project_id": str(row["logical_project_id"]),
            "mode": str(row["mode"]),
            "input_hash": str(row["input_hash"]),
            "provider_names": _value(row["provider_names_json"], []),
            "snapshot_ids": _value(row["snapshot_ids_json"], []),
            "status": str(row["status"]),
            "counts": _value(row["counts_json"], {}),
            "error": row["error"],
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
        }

    def list_verifications(
        self,
        *,
        logical_project_id: str,
        raw_project_ids: Sequence[str],
        status: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        ids = sorted({str(item) for item in raw_project_ids if str(item)})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        clauses = [f"c.project_id IN ({placeholders})"]
        params: list[Any] = list(ids)
        if status:
            clauses.append("b.status=?")
            params.append(status)
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT b.binding_id,b.card_version_id,v.card_id,c.project_id,c.task_id,v.statement,b.role,b.path,b.symbol,b.qualified_symbol,b.normalized_target,b.status,b.snapshot_id,b.updated_at "
                "FROM memory_card_bindings b JOIN memory_card_versions v ON v.card_version_id=b.card_version_id "
                "JOIN memory_cards c ON c.card_id=v.card_id AND c.current_version_id=v.card_version_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY b.updated_at DESC,b.binding_id LIMIT ?",
                (*params, max(1, min(int(limit), 10_000))),
            ).fetchall()
        return [
            {
                "binding_id": str(row["binding_id"]),
                "card_id": str(row["card_id"]),
                "card_version_id": str(row["card_version_id"]),
                "project_id": str(row["project_id"]),
                "task_id": row["task_id"],
                "statement": str(row["statement"]),
                "role": str(row["role"]),
                "path": row["path"],
                "symbol": row["symbol"],
                "qualified_symbol": row["qualified_symbol"],
                "normalized_target": str(row["normalized_target"]),
                "status": str(row["status"]),
                "snapshot_id": row["snapshot_id"],
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    def verification_history(self, binding_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM binding_verifications WHERE binding_id=? ORDER BY checked_at DESC,verification_id DESC LIMIT ?",
                (binding_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [
            {
                "verification_id": str(row["verification_id"]),
                "run_id": str(row["run_id"]),
                "binding_id": str(row["binding_id"]),
                "snapshot_id": str(row["snapshot_id"]),
                "status": str(row["status"]),
                "score": float(row["score"]),
                "resolved_path": row["resolved_path"],
                "resolved_symbol": row["resolved_symbol"],
                "evidence": _value(row["evidence_json"], {}),
                "reasons": _value(row["reasons_json"], []),
                "applied": bool(row["applied"]),
                "checked_at": str(row["checked_at"]),
            }
            for row in rows
        ]

    def report(
        self,
        *,
        logical_project_id: str,
        raw_project_ids: Sequence[str],
        limit: int = 20,
        include_manifest: bool = False,
    ) -> dict[str, Any]:
        ids = sorted({str(item) for item in raw_project_ids if str(item)})
        placeholders = ",".join("?" for _ in ids)
        current_counts: Counter[str] = Counter()
        historical_counts: Counter[str] = Counter()
        total_bindings = 0
        total_checks = 0
        if ids:
            with self.db.connection() as conn:
                rows = conn.execute(
                    "SELECT b.status,COUNT(*) AS n FROM memory_card_bindings b "
                    "JOIN memory_card_versions v ON v.card_version_id=b.card_version_id "
                    "JOIN memory_cards c ON c.card_id=v.card_id AND c.current_version_id=v.card_version_id "
                    f"WHERE c.project_id IN ({placeholders}) GROUP BY b.status",
                    ids,
                ).fetchall()
                current_counts.update({str(row["status"]): int(row["n"]) for row in rows})
                total_bindings = sum(current_counts.values())
                rows = conn.execute(
                    "SELECT bv.status,COUNT(*) AS n FROM binding_verifications bv "
                    "JOIN memory_card_bindings b ON b.binding_id=bv.binding_id "
                    "JOIN memory_card_versions v ON v.card_version_id=b.card_version_id "
                    "JOIN memory_cards c ON c.card_id=v.card_id "
                    f"WHERE c.project_id IN ({placeholders}) GROUP BY bv.status",
                    ids,
                ).fetchall()
                historical_counts.update({str(row["status"]): int(row["n"]) for row in rows})
                total_checks = sum(historical_counts.values())
        return {
            "logical_project_id": logical_project_id,
            "raw_project_ids": ids,
            "bindings": {"total": total_bindings, "statuses": dict(current_counts)},
            "checks": {"total": total_checks, "statuses": dict(historical_counts)},
            "snapshots": self.snapshots_for_project(
                logical_project_id,
                limit=limit,
                include_manifest=include_manifest,
            ),
            "latest_runs": self.latest_runs(logical_project_id=logical_project_id, limit=limit),
        }
