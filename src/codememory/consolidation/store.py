"""SQLite persistence for versioned coding-memory cards.

The store deliberately contains no matching policy.  ``ConsolidationService``
decides whether a candidate creates, merges, or versions a card; this module
only performs validated, transactional writes and read-side projections.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from ..storage.database import Database


CARD_KINDS = {
    "route_observation",
    "decision",
    "convention",
    "failure",
    "validation",
    "preference",
    "anti_binding",
}
CARD_STATUSES = {
    "proposed",
    "verified",
    "stable",
    "uncertain",
    "stale",
    "superseded",
    "rejected",
}
BOUNDING_STATUSES = {"unverified", "verified", "stale", "missing", "renamed", "rejected"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _value(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _short_id(prefix: str, *parts: str, length: int = 24) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}-{digest}"


class CardStore:
    """Read and write API for cards, versions, bindings, and relations."""

    def __init__(self, database: Database):
        self.db = database
        self.db.ensure_initialized()

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        candidate_id = str(row["candidate_id"])
        evidence = [
            str(item[0])
            for item in conn.execute(
                "SELECT event_id FROM memory_candidate_evidence WHERE candidate_id=? ORDER BY ordinal",
                (candidate_id,),
            ).fetchall()
        ]
        return {
            "candidate_id": candidate_id,
            "run_id": str(row["run_id"]),
            "project_id": str(row["project_id"]),
            "task_id": str(row["task_id"]),
            "session_id": row["session_id"],
            "kind": str(row["kind"]),
            "statement": str(row["statement"]),
            "aliases": _value(row["aliases_json"], []),
            "bindings": _value(row["bindings_json"], []),
            "evidence_event_ids": evidence,
            "confidence": float(row["confidence"]),
            "uncertainty": str(row["uncertainty"]),
            "lifecycle_hint": str(row["lifecycle_hint"]),
            "relation_hints": _value(row["relation_hints_json"], []),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_candidates(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        candidate_ids: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return immutable candidate proposals in deterministic order."""

        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        ids = [str(value) for value in (candidate_ids or []) if str(value)]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"candidate_id IN ({placeholders})")
            params.extend(ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 100_000))
        with self.db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM memory_candidates {where} ORDER BY created_at, candidate_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._candidate_from_row(row, conn) for row in rows]

    @staticmethod
    def _binding_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "binding_id": str(row["binding_id"]),
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

    def _version_from_row(self, row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        version_id = str(row["card_version_id"])
        bindings = [
            self._binding_from_row(binding)
            for binding in conn.execute(
                "SELECT * FROM memory_card_bindings WHERE card_version_id=? "
                "ORDER BY role, normalized_target",
                (version_id,),
            ).fetchall()
        ]
        evidence_rows = conn.execute(
            "SELECT candidate_id,event_id,relation_type,ordinal FROM memory_card_evidence "
            "WHERE card_version_id=? ORDER BY ordinal,candidate_id,event_id",
            (version_id,),
        ).fetchall()
        evidence = [
            {
                "candidate_id": str(item["candidate_id"]),
                "event_id": str(item["event_id"]),
                "relation_type": str(item["relation_type"]),
                "ordinal": int(item["ordinal"]),
            }
            for item in evidence_rows
        ]
        return {
            "card_version_id": version_id,
            "card_id": str(row["card_id"]),
            "version_no": int(row["version_no"]),
            "statement": str(row["statement"]),
            "aliases": _value(row["aliases_json"], []),
            "confidence": float(row["confidence"]),
            "uncertainty": str(row["uncertainty"]),
            "source_candidate_ids": _value(row["source_candidate_ids_json"], []),
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "supersedes_version_id": row["supersedes_version_id"],
            "change_reason": str(row["change_reason"]),
            "created_at": str(row["created_at"]),
            "bindings": bindings,
            "evidence": evidence,
        }

    @staticmethod
    def _card_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "card_id": str(row["card_id"]),
            "project_id": str(row["project_id"]),
            "task_id": row["task_id"],
            "kind": str(row["kind"]),
            "canonical_key": str(row["canonical_key"]),
            "status": str(row["status"]),
            "confidence": float(row["confidence"]),
            "current_version_id": row["current_version_id"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "metadata": _value(row["metadata_json"], {}),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "version_no": int(row["version_no"]) if row["version_no"] is not None else None,
            "statement": row["statement"],
            "aliases": _value(row["aliases_json"], []) if row["aliases_json"] is not None else [],
        }

    @staticmethod
    def _card_quality(conn: sqlite3.Connection, card_id: str) -> dict[str, Any]:
        """Summarize source-candidate gate decisions without changing cards."""

        rows = conn.execute(
            "SELECT q.decision,q.quality_score,q.reasons_json,q.classifier_version "
            "FROM candidate_quality_reviews q "
            "JOIN memory_card_evidence e ON e.candidate_id=q.candidate_id "
            "JOIN memory_card_versions v ON v.card_version_id=e.card_version_id "
            "WHERE v.card_id=?",
            (card_id,),
        ).fetchall()
        if not rows:
            return {
                "decision": "legacy_unreviewed",
                "reviewed_candidates": 0,
                "scores": [],
                "reasons": [],
                "classifier_versions": [],
            }
        decisions = {str(row["decision"]) for row in rows}
        decision = (
            "quarantine"
            if "quarantine" in decisions
            else "review"
            if "review" in decisions
            else "accepted"
        )
        reasons: list[str] = []
        for row in rows:
            try:
                values = json.loads(str(row["reasons_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                values = []
            for reason in values if isinstance(values, list) else []:
                text = str(reason)
                if text and text not in reasons:
                    reasons.append(text)
                if len(reasons) >= 12:
                    break
            if len(reasons) >= 12:
                break
        return {
            "decision": decision,
            "reviewed_candidates": len(rows),
            "scores": [float(row["quality_score"]) for row in rows],
            "reasons": reasons,
            "classifier_versions": sorted({str(row["classifier_version"]) for row in rows}),
        }

    @staticmethod
    def _quality_visibility_clause(
        *, card_alias: str = "c", include_quarantine: bool = False
    ) -> str:
        """Return the default card visibility guard.

        Quality is a side projection, so legacy cards without a review remain
        visible (and are labelled ``legacy_unreviewed``).  Only an explicit
        quarantine review hides a card from normal listing/search/graph
        results.  Callers that are auditing history can opt in to all cards.
        """

        if include_quarantine:
            return ""
        return (
            "NOT EXISTS (SELECT 1 FROM memory_card_evidence qce "
            "JOIN memory_card_versions qcv ON qcv.card_version_id=qce.card_version_id "
            "JOIN candidate_quality_reviews qcr ON qcr.candidate_id=qce.candidate_id "
            f"WHERE qcv.card_id={card_alias}.card_id AND qcr.decision='quarantine')"
        )

    def list_cards(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        include_quarantine: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("c.project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append("(c.task_id=? OR EXISTS (SELECT 1 FROM memory_card_evidence e "
                           "JOIN memory_card_versions ev ON ev.card_version_id=e.card_version_id "
                           "WHERE ev.card_id=c.card_id AND e.candidate_id IN "
                           "(SELECT candidate_id FROM memory_candidates WHERE task_id=?)))")
            params.extend([task_id, task_id])
        if status:
            if status not in CARD_STATUSES:
                raise ValueError(f"unknown card status: {status}")
            clauses.append("c.status=?")
            params.append(status)
        if kind:
            if kind not in CARD_KINDS:
                raise ValueError(f"unknown card kind: {kind}")
            clauses.append("c.kind=?")
            params.append(kind)
        quality_clause = self._quality_visibility_clause(
            card_alias="c", include_quarantine=include_quarantine
        )
        if quality_clause:
            clauses.append(quality_clause)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 10_000))
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT c.*, v.version_no, v.statement, v.aliases_json "
                "FROM memory_cards c LEFT JOIN memory_card_versions v "
                "ON v.card_version_id=c.current_version_id "
                f"{where} ORDER BY c.updated_at DESC, c.card_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            result = []
            for row in rows:
                summary = self._card_summary(row)
                summary["quality"] = self._card_quality(conn, summary["card_id"])
                result.append(summary)
            return result

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM memory_cards WHERE card_id=?", (card_id,)).fetchone()
            if row is None:
                return None
            summary = {
                "card_id": str(row["card_id"]),
                "project_id": str(row["project_id"]),
                "task_id": row["task_id"],
                "kind": str(row["kind"]),
                "canonical_key": str(row["canonical_key"]),
                "status": str(row["status"]),
                "confidence": float(row["confidence"]),
                "current_version_id": row["current_version_id"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "metadata": _value(row["metadata_json"], {}),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            summary["quality"] = self._card_quality(conn, card_id)
            versions = [
                self._version_from_row(version, conn)
                for version in conn.execute(
                    "SELECT * FROM memory_card_versions WHERE card_id=? ORDER BY version_no DESC",
                    (card_id,),
                ).fetchall()
            ]
            links = [
                {
                    "card_version_id": str(link["card_version_id"]),
                    "target_type": str(link["target_type"]),
                    "target_id": str(link["target_id"]),
                    "relation_type": str(link["relation_type"]),
                    "metadata": _value(link["metadata_json"], {}),
                    "created_at": str(link["created_at"]),
                }
                for link in conn.execute(
                    "SELECT * FROM memory_card_links WHERE card_version_id IN "
                    "(SELECT card_version_id FROM memory_card_versions WHERE card_id=?) "
                    "ORDER BY created_at, target_type, target_id",
                    (card_id,),
                ).fetchall()
            ]
            lifecycle = [
                {
                    "lifecycle_event_id": str(item["lifecycle_event_id"]),
                    "from_status": item["from_status"],
                    "to_status": str(item["to_status"]),
                    "actor": str(item["actor"]),
                    "reason": str(item["reason"]),
                    "metadata": _value(item["metadata_json"], {}),
                    "created_at": str(item["created_at"]),
                }
                for item in conn.execute(
                    "SELECT * FROM memory_card_lifecycle_events WHERE card_id=? ORDER BY created_at",
                    (card_id,),
                ).fetchall()
            ]
            decisions = [
                {
                    "decision_id": str(item["decision_id"]),
                    "candidate_id": str(item["candidate_id"]),
                    "action": str(item["action"]),
                    "card_version_id": item["card_version_id"],
                    "score": item["score"],
                    "reason": str(item["reason"]),
                    "metadata": _value(item["metadata_json"], {}),
                    "created_at": str(item["created_at"]),
                }
                for item in conn.execute(
                    "SELECT * FROM consolidation_decisions WHERE card_id=? ORDER BY created_at",
                    (card_id,),
                ).fetchall()
            ]
            summary.update({"versions": versions, "links": links, "lifecycle": lifecycle, "decisions": decisions})
            return summary

    def card_history(self, card_id: str) -> list[dict[str, Any]]:
        card = self.get_card(card_id)
        return list(card["versions"]) if card else []

    def card_relations(self, card_id: str) -> list[dict[str, Any]]:
        card = self.get_card(card_id)
        return list(card["links"]) if card else []

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        include_quarantine: bool = False,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 200))
        clauses = []
        params: list[Any] = [query]
        if project_id:
            clauses.append("c.project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append(
                "(c.task_id=? OR EXISTS (SELECT 1 FROM memory_card_evidence e "
                "JOIN memory_card_versions ev ON ev.card_version_id=e.card_version_id "
                "JOIN memory_candidates mc ON mc.candidate_id=e.candidate_id "
                "WHERE ev.card_id=c.card_id AND mc.task_id=?))"
            )
            params.extend([task_id, task_id])
        if status:
            if status not in CARD_STATUSES:
                raise ValueError(f"unknown card status: {status}")
            clauses.append("c.status=?")
            params.append(status)
        quality_clause = self._quality_visibility_clause(
            card_alias="c", include_quarantine=include_quarantine
        )
        if quality_clause:
            clauses.append(quality_clause)
        suffix = (" AND " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self.db.connection() as conn:
            try:
                rows = conn.execute(
                    "SELECT c.*, v.version_no, v.statement, v.aliases_json "
                    "FROM memory_card_fts f JOIN memory_cards c ON c.card_id=f.card_id "
                    "LEFT JOIN memory_card_versions v ON v.card_version_id=c.current_version_id "
                    f"WHERE memory_card_fts MATCH ?{suffix} ORDER BY c.updated_at DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
            except sqlite3.OperationalError:
                literal = '"' + query.replace('"', '""') + '"'
                params[0] = literal
                rows = conn.execute(
                    "SELECT c.*, v.version_no, v.statement, v.aliases_json "
                    "FROM memory_card_fts f JOIN memory_cards c ON c.card_id=f.card_id "
                    "LEFT JOIN memory_card_versions v ON v.card_version_id=c.current_version_id "
                    f"WHERE memory_card_fts MATCH ?{suffix} ORDER BY c.updated_at DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
            if not rows:
                # Literal fallback is useful for contiguous CJK phrases and
                # punctuation-heavy paths when unicode61 does not tokenize as
                # the user expects.
                like_clauses = [
                    "instr(v.statement, ?) > 0",
                    "instr(v.aliases_json, ?) > 0",
                    "EXISTS (SELECT 1 FROM memory_card_bindings b WHERE b.card_version_id=v.card_version_id AND "
                    "(instr(coalesce(b.path,''), ?) > 0 OR instr(coalesce(b.symbol,''), ?) > 0))",
                ]
                fallback_params: list[Any] = [query, query, query, query]
                if project_id:
                    fallback_params.append(project_id)
                if task_id:
                    fallback_params.extend([task_id, task_id])
                if status:
                    fallback_params.append(status)
                fallback_params.append(limit)
                extra = ""
                if project_id:
                    extra += " AND c.project_id=?"
                if task_id:
                    extra += (
                        " AND (c.task_id=? OR EXISTS (SELECT 1 FROM memory_card_evidence e "
                        "JOIN memory_card_versions ev ON ev.card_version_id=e.card_version_id "
                        "JOIN memory_candidates mc ON mc.candidate_id=e.candidate_id "
                        "WHERE ev.card_id=c.card_id AND mc.task_id=?))"
                    )
                if status:
                    extra += " AND c.status=?"
                if quality_clause:
                    extra += " AND " + quality_clause
                rows = conn.execute(
                    "SELECT c.*, v.version_no, v.statement, v.aliases_json "
                    "FROM memory_cards c LEFT JOIN memory_card_versions v ON v.card_version_id=c.current_version_id "
                    "WHERE (" + " OR ".join(like_clauses) + ")" + extra
                    + " ORDER BY c.updated_at DESC LIMIT ?",
                    tuple(fallback_params),
                ).fetchall()
            result = []
            for row in rows:
                summary = self._card_summary(row)
                summary["quality"] = self._card_quality(conn, summary["card_id"])
                result.append(summary)
            return result

    # ---- transactional write helpers used by ConsolidationService ----

    @staticmethod
    def candidate_fingerprint(candidate: dict[str, Any]) -> str:
        payload = {
            "project_id": candidate["project_id"],
            "task_id": candidate["task_id"],
            "kind": candidate["kind"],
            "statement": candidate["statement"],
            "aliases": candidate.get("aliases") or [],
            "bindings": candidate.get("bindings") or [],
            "evidence_event_ids": candidate.get("evidence_event_ids") or [],
        }
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def decision_key(candidate: dict[str, Any], policy_version: str) -> str:
        return hashlib.sha256(
            f"{policy_version}\n{candidate['candidate_id']}\n{CardStore.candidate_fingerprint(candidate)}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def normalized_target(binding: dict[str, Any]) -> str:
        values = [binding.get("path"), binding.get("qualified_symbol"), binding.get("symbol")]
        value = next((str(item).strip() for item in values if item), "")
        return value.replace("\\", "/").casefold()

    @staticmethod
    def new_card_id(project_id: str, kind: str, canonical_key: str) -> str:
        return _short_id("card", project_id, kind, canonical_key)

    @staticmethod
    def new_version_id(card_id: str, version_no: int) -> str:
        return f"{card_id}:v{version_no}"

    def existing_decision(self, conn: sqlite3.Connection, decision_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM consolidation_decisions WHERE decision_key=?", (decision_key,)
        ).fetchone()

    def insert_card(
        self,
        conn: sqlite3.Connection,
        *,
        card_id: str,
        project_id: str,
        task_id: str | None,
        kind: str,
        canonical_key: str,
        status: str,
        confidence: float,
        statement: str,
        aliases: list[str],
        uncertainty: str,
        candidate_id: str,
        evidence_event_ids: list[str],
        bindings: list[dict[str, Any]],
        reason: str,
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> tuple[str, str]:
        if kind not in CARD_KINDS:
            raise ValueError(f"unsupported card kind: {kind}")
        if status not in CARD_STATUSES:
            raise ValueError(f"unsupported card status: {status}")
        now = _now()
        version_id = self.new_version_id(card_id, 1)
        conn.execute(
            "INSERT INTO memory_cards(card_id,project_id,task_id,kind,canonical_key,status,confidence,current_version_id,valid_from,valid_until,metadata_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                card_id,
                project_id,
                task_id,
                kind,
                canonical_key,
                status,
                max(0.0, min(1.0, float(confidence))),
                version_id,
                valid_from,
                valid_until,
                _json({"created_by": "consolidator-v1"}),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO memory_card_versions(card_version_id,card_id,version_no,statement,aliases_json,confidence,uncertainty,source_candidate_ids_json,valid_from,valid_until,supersedes_version_id,change_reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                card_id,
                1,
                statement,
                _json(_unique_strings(aliases)),
                max(0.0, min(1.0, float(confidence))),
                uncertainty,
                _json([candidate_id]),
                valid_from,
                valid_until,
                None,
                reason,
                now,
            ),
        )
        self.add_evidence(conn, version_id, candidate_id, evidence_event_ids)
        self.add_bindings(conn, version_id, bindings)
        self.refresh_fts(conn, card_id)
        return card_id, version_id

    def append_version(
        self,
        conn: sqlite3.Connection,
        *,
        card: sqlite3.Row,
        current_version: sqlite3.Row,
        statement: str,
        aliases: list[str],
        confidence: float,
        uncertainty: str,
        candidate_id: str,
        evidence_event_ids: list[str],
        bindings: list[dict[str, Any]],
        reason: str,
    ) -> tuple[str, int]:
        card_id = str(card["card_id"])
        version_no = int(current_version["version_no"]) + 1
        version_id = self.new_version_id(card_id, version_no)
        now = _now()
        conn.execute(
            "INSERT INTO memory_card_versions(card_version_id,card_id,version_no,statement,aliases_json,confidence,uncertainty,source_candidate_ids_json,valid_from,valid_until,supersedes_version_id,change_reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                card_id,
                version_no,
                statement,
                _json(_unique_strings(aliases)),
                max(0.0, min(1.0, float(confidence))),
                uncertainty,
                _json([candidate_id]),
                card["valid_from"],
                card["valid_until"],
                str(current_version["card_version_id"]),
                reason,
                now,
            ),
        )
        # Close the validity interval of the superseded version so temporal
        # readers never see two open versions for one card.
        conn.execute(
            "UPDATE memory_card_versions SET valid_until=? "
            "WHERE card_version_id=? AND valid_until IS NULL",
            (now, str(current_version["card_version_id"])),
        )
        self.add_evidence(conn, version_id, candidate_id, evidence_event_ids)
        self.add_bindings(conn, version_id, bindings)
        conn.execute(
            "INSERT OR IGNORE INTO memory_card_links(card_version_id,target_type,target_id,relation_type,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
            (version_id, "card_version", str(current_version["card_version_id"]), "supersedes", "{}", now),
        )
        conn.execute(
            "UPDATE memory_cards SET current_version_id=?, confidence=?, updated_at=? WHERE card_id=?",
            (version_id, max(float(card["confidence"]), float(confidence)), now, card_id),
        )
        self.refresh_fts(conn, card_id)
        return version_id, version_no

    def add_evidence(
        self,
        conn: sqlite3.Connection,
        version_id: str,
        candidate_id: str,
        event_ids: Iterable[str],
        *,
        relation_type: str = "supports",
    ) -> None:
        for ordinal, event_id in enumerate(dict.fromkeys(str(item) for item in event_ids if str(item))):
            conn.execute(
                "INSERT OR IGNORE INTO memory_card_evidence(card_version_id,candidate_id,event_id,relation_type,ordinal) VALUES (?,?,?,?,?)",
                (version_id, candidate_id, event_id, relation_type, ordinal),
            )
            conn.execute(
                "INSERT OR IGNORE INTO memory_card_links(card_version_id,target_type,target_id,relation_type,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
                (version_id, "event", event_id, relation_type, "{}", _now()),
            )
        conn.execute(
            "INSERT OR IGNORE INTO memory_card_links(card_version_id,target_type,target_id,relation_type,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
            (version_id, "candidate", candidate_id, "derived_from", "{}", _now()),
        )

    def add_bindings(
        self,
        conn: sqlite3.Connection,
        version_id: str,
        bindings: Iterable[dict[str, Any]],
    ) -> None:
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            path = str(binding.get("path") or "") or None
            symbol = str(binding.get("symbol") or "") or None
            qualified = str(binding.get("qualified_symbol") or "") or None
            target = self.normalized_target(binding)
            if not target:
                continue
            role = str(binding.get("role") or "supporting_symbol")
            status = str(binding.get("status") or "unverified")
            if status not in BOUNDING_STATUSES:
                status = "unverified"
            binding_id = _short_id("binding", version_id, role, target)
            evidence = _unique_strings(binding.get("evidence") or binding.get("evidence_event_ids") or [])
            now = _now()
            conn.execute(
                "INSERT OR IGNORE INTO memory_card_bindings(binding_id,card_version_id,role,path,symbol,qualified_symbol,normalized_target,status,snapshot_id,evidence_event_ids_json,metadata_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    binding_id,
                    version_id,
                    role,
                    path,
                    symbol,
                    qualified,
                    target,
                    status,
                    binding.get("snapshot_id"),
                    _json(evidence),
                    _json(binding.get("metadata") or {}),
                    now,
                    now,
                ),
            )
            target_type = "file" if path else "symbol"
            target_id = path or qualified or symbol
            if target_id:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_card_links(card_version_id,target_type,target_id,relation_type,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
                    (version_id, target_type, target_id, role, "{}", now),
                )

    def add_link(
        self,
        conn: sqlite3.Connection,
        version_id: str,
        target_type: str,
        target_id: str,
        relation_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if target_type not in {"card", "card_version", "candidate", "event", "file", "symbol", "task"}:
            raise ValueError(f"unsupported card link target type: {target_type}")
        conn.execute(
            "INSERT OR IGNORE INTO memory_card_links(card_version_id,target_type,target_id,relation_type,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
            (version_id, target_type, target_id, relation_type, _json(metadata or {}), _now()),
        )

    def record_decision(
        self,
        conn: sqlite3.Connection,
        *,
        decision_key: str,
        project_id: str,
        task_id: str,
        candidate_id: str,
        action: str,
        card_id: str | None,
        card_version_id: str | None,
        score: float | None,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        decision_id = _short_id("decision", decision_key)
        conn.execute(
            "INSERT OR IGNORE INTO consolidation_decisions(decision_id,decision_key,project_id,task_id,candidate_id,action,card_id,card_version_id,score,reason,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                decision_key,
                project_id,
                task_id,
                candidate_id,
                action,
                card_id,
                card_version_id,
                score,
                reason,
                _json(metadata or {}),
                _now(),
            ),
        )
        return decision_id

    def refresh_fts(self, conn: sqlite3.Connection, card_id: str) -> None:
        conn.execute("DELETE FROM memory_card_fts WHERE card_id=?", (card_id,))
        row = conn.execute(
            "SELECT c.card_id,c.project_id,c.task_id,c.status,c.current_version_id,v.statement,v.aliases_json "
            "FROM memory_cards c LEFT JOIN memory_card_versions v ON v.card_version_id=c.current_version_id "
            "WHERE c.card_id=?",
            (card_id,),
        ).fetchone()
        if row is None:
            return
        version_id = row["current_version_id"]
        bindings = conn.execute(
            "SELECT path,symbol,qualified_symbol FROM memory_card_bindings WHERE card_version_id=?",
            (version_id,),
        ).fetchall()
        pieces = [str(row["status"]), str(row["statement"] or ""), str(_value(row["aliases_json"], []))]
        pieces.extend(
            str(value)
            for binding in bindings
            for value in (binding["path"], binding["symbol"], binding["qualified_symbol"])
            if value
        )
        conn.execute(
            "INSERT INTO memory_card_fts(card_id,project_id,task_id,text) VALUES (?,?,?,?)",
            (card_id, row["project_id"], row["task_id"] or "", " ".join(pieces)),
        )

    def rebuild_search_index(self) -> int:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM memory_card_fts")
            card_ids = [str(row[0]) for row in conn.execute("SELECT card_id FROM memory_cards")]
            for card_id in card_ids:
                self.refresh_fts(conn, card_id)
            return len(card_ids)

    def extend_graph(
        self,
        graph: dict[str, Any],
        *,
        task_id: str | None = None,
        limit: int = 300,
        include_quarantine: bool = False,
    ) -> dict[str, Any]:
        """Add card/version/binding nodes to the existing candidate graph."""

        nodes: dict[str, dict[str, Any]] = {
            str(node["id"]): dict(node)
            for node in graph.get("nodes", [])
            if isinstance(node, dict) and node.get("id")
        }
        edges: dict[tuple[str, str, str], dict[str, Any]] = {
            (str(edge.get("source")), str(edge.get("target")), str(edge.get("relation"))): dict(edge)
            for edge in graph.get("edges", [])
            if isinstance(edge, dict) and edge.get("source") and edge.get("target") and edge.get("relation")
        }

        def add_node(node_id: str, kind: str, label: str, **metadata: Any) -> None:
            if node_id not in nodes:
                nodes[node_id] = {"id": node_id, "kind": kind, "label": label, **metadata}

        def add_edge(source: str, target: str, relation: str, **metadata: Any) -> None:
            key = (source, target, relation)
            edges.setdefault(key, {"source": source, "target": target, "relation": relation, **metadata})

        limit = max(1, min(int(limit), 2000))
        with self.db.connection() as conn:
            params: list[Any] = []
            where = ""
            if task_id:
                where = (
                    "WHERE (c.task_id=? OR EXISTS (SELECT 1 FROM memory_card_versions ev "
                    "JOIN memory_card_evidence ce ON ce.card_version_id=ev.card_version_id "
                    "JOIN memory_candidates mc ON mc.candidate_id=ce.candidate_id "
                    "WHERE ev.card_id=c.card_id AND mc.task_id=?))"
                )
                params.extend([task_id, task_id])
            quality_clause = self._quality_visibility_clause(
                card_alias="c", include_quarantine=include_quarantine
            )
            if quality_clause:
                where = (where + " AND " if where else "WHERE ") + quality_clause
            cards = conn.execute(
                "SELECT c.*,v.version_no,v.statement,v.aliases_json "
                "FROM memory_cards c LEFT JOIN memory_card_versions v ON v.card_version_id=c.current_version_id "
                f"{where} ORDER BY c.updated_at DESC,c.card_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            for card in cards:
                cid = str(card["card_id"])
                node_id = f"card:{cid}"
                add_node(
                    node_id,
                    "card",
                    _trim(str(card["statement"] or cid), 120),
                    card_id=cid,
                    card_kind=str(card["kind"]),
                    status=str(card["status"]),
                    confidence=float(card["confidence"]),
                    version_no=int(card["version_no"]) if card["version_no"] is not None else None,
                    aliases=_value(card["aliases_json"], []),
                    quality=self._card_quality(conn, cid),
                )
                if card["task_id"]:
                    task_node = f"task:{card['task_id']}"
                    if task_node in nodes:
                        add_edge(task_node, node_id, "contains_card")
                # A card may merge evidence from several tasks while keeping
                # its first task as the stable owner field.  Link every
                # contributing task that is present in this bounded graph so
                # task-scoped views do not make a shared card look orphaned.
                contributing_tasks = conn.execute(
                    "SELECT DISTINCT mc.task_id FROM memory_card_evidence ce "
                    "JOIN memory_candidates mc ON mc.candidate_id=ce.candidate_id "
                    "JOIN memory_card_versions cv ON cv.card_version_id=ce.card_version_id "
                    "WHERE cv.card_id=?",
                    (cid,),
                ).fetchall()
                for task_row in contributing_tasks:
                    task_id_value = str(task_row["task_id"])
                    task_node = f"task:{task_id_value}"
                    if task_node in nodes:
                        add_edge(task_node, node_id, "contributes_card")
                version_id = card["current_version_id"]
                if not version_id:
                    continue
                bindings = conn.execute(
                    "SELECT path,symbol,qualified_symbol,role,status FROM memory_card_bindings WHERE card_version_id=?",
                    (version_id,),
                ).fetchall()
                for binding in bindings:
                    if binding["path"]:
                        target = f"file:{binding['path']}"
                        add_node(target, "file", str(binding["path"]), path=str(binding["path"]))
                        add_edge(node_id, target, str(binding["role"]))
                    symbol = binding["qualified_symbol"] or binding["symbol"]
                    if symbol:
                        target = f"symbol:{symbol}"
                        add_node(target, "symbol", str(symbol), symbol=str(symbol), binding_status=str(binding["status"]))
                        add_edge(node_id, target, str(binding["role"]))
                links = conn.execute(
                    "SELECT target_type,target_id,relation_type,metadata_json FROM memory_card_links WHERE card_version_id=?",
                    (version_id,),
                ).fetchall()
                for link in links:
                    target_type = str(link["target_type"])
                    target_id = str(link["target_id"])
                    if target_type == "card":
                        target = f"card:{target_id}"
                        add_edge(node_id, target, str(link["relation_type"]))
                    elif target_type == "card_version":
                        target = f"card-version:{target_id}"
                        add_node(target, "card_version", target_id, card_version_id=target_id)
                        add_edge(node_id, target, str(link["relation_type"]))
                    elif target_type == "candidate":
                        target = f"candidate:{target_id}"
                        if target in nodes:
                            add_edge(node_id, target, str(link["relation_type"]))
                    elif target_type == "event":
                        target = f"event:{target_id}"
                        if target in nodes:
                            add_edge(node_id, target, str(link["relation_type"]))
                    elif target_type in {"file", "symbol", "task"}:
                        target = f"{target_type}:{target_id}"
                        if target_type == "file":
                            add_node(target, "file", target_id, path=target_id)
                        elif target_type == "symbol":
                            add_node(target, "symbol", target_id, symbol=target_id)
                        if target in nodes:
                            add_edge(node_id, target, str(link["relation_type"]))
        # A bounded graph may omit a relation target (for example a card from
        # another task).  Never return dangling edges: the 3D client treats
        # the projection as a closed graph and should not have to guess which
        # nodes were intentionally truncated.
        graph["nodes"] = list(nodes.values())
        graph["edges"] = [
            edge
            for edge in edges.values()
            if edge.get("source") in nodes and edge.get("target") in nodes
        ]
        graph["card_count"] = sum(1 for node in nodes.values() if node.get("kind") == "card")
        return graph

    def transition(
        self,
        card_id: str,
        to_status: str,
        *,
        reason: str,
        actor: str = "operator",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if to_status not in CARD_STATUSES:
            raise ValueError(f"unknown card status: {to_status}")
        if not reason.strip():
            raise ValueError("transition reason is required")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM memory_cards WHERE card_id=?", (card_id,)).fetchone()
            if row is None:
                return {"updated": False, "card_id": card_id, "reason": "not_found"}
            from_status = str(row["status"])
            if from_status == to_status:
                return {"updated": False, "card_id": card_id, "status": to_status, "reason": "already_in_state"}
            allowed = {
                "proposed": {"verified", "stable", "uncertain", "rejected", "stale"},
                "verified": {"stable", "stale", "superseded", "uncertain", "rejected"},
                "stable": {"stale", "superseded", "rejected"},
                "uncertain": {"proposed", "verified", "stable", "rejected", "stale"},
                "stale": {"verified", "stable", "superseded", "rejected"},
                "superseded": set(),
                "rejected": {"proposed", "uncertain"},
            }
            if to_status not in allowed.get(from_status, set()):
                raise ValueError(f"illegal card transition {from_status}->{to_status}")
            now = _now()
            conn.execute(
                "UPDATE memory_cards SET status=?, updated_at=? WHERE card_id=?",
                (to_status, now, card_id),
            )
            conn.execute(
                "INSERT INTO memory_card_lifecycle_events(lifecycle_event_id,card_id,from_status,to_status,actor,reason,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), card_id, from_status, to_status, actor, reason, _json(metadata or {}), now),
            )
            self.refresh_fts(conn, card_id)
            return {"updated": True, "card_id": card_id, "from_status": from_status, "status": to_status, "reason": reason}


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _trim(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"
