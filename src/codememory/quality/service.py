"""Application service for the continuous coding-memory quality gate."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from typing import Any, Iterable, Mapping

from ..storage.repository import MemoryRepository, json_text, json_value
from .classifier import classify_event, review_candidate
from .models import (
    QUALITY_CLASSIFIER_VERSION,
    CandidateQualityReview,
    EventQualityEvaluation,
    QualityRunResult,
)
from .project_scope import logical_identity_key, root_basename, normalize_root
from .store import QualityStore


class QualityService:
    """Coordinate deterministic classification and replayable persistence.

    Quality is a derived projection.  Every public method keeps canonical
    events, candidates, and card history untouched; failures are recorded on a
    ``quality_runs`` row and can be retried with the same input hash.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        store: QualityStore | None = None,
        classifier_version: str = QUALITY_CLASSIFIER_VERSION,
    ) -> None:
        self.repository = repository
        self.store = store or QualityStore(repository.db)
        self.classifier_version = classifier_version

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return MemoryRepository._event_from_row(row)

    @staticmethod
    def _evaluation_from_dict(value: Mapping[str, Any]) -> EventQualityEvaluation:
        return EventQualityEvaluation(
            event_id=str(value["event_id"]),
            project_id=str(value["project_id"]),
            task_id=str(value["task_id"]),
            role=str(value["role"]),
            decision=str(value["decision"]),
            signal_score=float(value["signal_score"]),
            dimensions=dict(value.get("dimensions") or {}),
            reasons=tuple(value.get("reasons") or ()),
            classifier_version=str(value["classifier_version"]),
            evaluated_at=value.get("evaluated_at"),
        )

    def resolve_project(
        self,
        *,
        project_id: str,
        root_path: str | None = None,
        repo_id: str | None = None,
        display_name: str | None = None,
        explicit_logical_project_id: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        root = normalize_root(root_path)
        name = display_name or root_basename(root) or project_id
        if not explicit_logical_project_id and not persist:
            # A reviewed alias is stronger than the root/name heuristic.  Use
            # the effective mapping when reporting a replay scope so counts
            # and filters reflect the same ownership users see in the store.
            existing = self.store.logical_project_for_raw(project_id)
            if existing:
                return {
                    "raw_project_id": project_id,
                    "logical_project_id": existing["logical_project_id"],
                    "identity_key": existing["identity_key"],
                    "display_name": existing["display_name"],
                    "normalized_root": root,
                    "confidence": 1.0,
                    "alias_type": "effective",
                    "inferred": False,
                }
        canonical_project_name = project_id.strip().casefold() in {"project_j", "projectj"}
        if explicit_logical_project_id:
            identity = f"explicit:{explicit_logical_project_id}"
            # Keep the explicit id stable by making the generated id an
            # alias only when it already exists.  The store's deterministic id
            # remains the canonical value for new mappings.
        elif canonical_project_name:
            # The hook adapter emits the stable raw ``project_j`` identity for
            # known Project_J roots.  Keep quality aliases on the canonical
            # name scope even when an optional repo_id is present.
            identity = "name:project_j"
        else:
            identity = logical_identity_key(
                root_path=root,
                repo_id=repo_id,
                display_name=name,
                infer_common_name=True,
            )
        alias_type = (
            "explicit"
            if explicit_logical_project_id or canonical_project_name
            else (
                "repo"
                if repo_id
                else "basename"
                if root_basename(root) in {"project_j", "projectj"}
                else "root"
            )
        )
        confidence = (
            1.0
            if explicit_logical_project_id or repo_id
            else 0.92
            if alias_type == "basename"
            else 1.0
        )
        if persist:
            resolution = self.store.ensure_logical_project(
                raw_project_id=project_id,
                identity_key=identity,
                display_name=name,
                normalized_root=root,
                alias_type=alias_type,
                alias_value=root or project_id,
                confidence=confidence,
                evidence={
                    "source": "event.context",
                    "repo_id": repo_id,
                    "root_path": root_path,
                },
            )
            return resolution.as_dict()
        from .project_scope import logical_project_id

        return {
            "raw_project_id": project_id,
            "logical_project_id": logical_project_id(identity),
            "identity_key": identity,
            "display_name": name,
            "normalized_root": root,
            "confidence": confidence,
            "alias_type": alias_type,
            "inferred": alias_type in {"basename", "inferred"},
        }

    def evaluate_event(
        self,
        event: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> EventQualityEvaluation:
        evaluation = classify_event(event, classifier_version=self.classifier_version)
        if persist:
            self.store.upsert_event_evaluation(evaluation)
            context = event.get("context") if isinstance(event.get("context"), Mapping) else {}
            self.resolve_project(
                project_id=str(event.get("project_id") or "unknown"),
                root_path=str(context.get("root_path") or context.get("cwd") or "") or None,
                repo_id=str(context.get("repo_id") or "") or None,
                persist=True,
            )
        return evaluation

    def evaluate_event_id(
        self, event_id: str, *, persist: bool = True
    ) -> EventQualityEvaluation | None:
        with self.repository.db.connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        return self.evaluate_event(self._event_from_row(row), persist=persist)

    def evaluate_events(
        self, events: Iterable[Mapping[str, Any]], *, persist: bool = True
    ) -> list[EventQualityEvaluation]:
        event_rows = list(events)
        evaluations = [
            classify_event(event, classifier_version=self.classifier_version)
            for event in event_rows
        ]
        if persist and evaluations:
            self.store.upsert_event_evaluations(evaluations)
            # Alias resolution is deliberately separate from the batch write;
            # it is small and gives each root its own provenance.  Resolve one
            # row per unique project/root/repository tuple; a history replay
            # can contain tens of thousands of events from the same checkout.
            seen_scopes: set[tuple[str, str, str]] = set()
            for event in event_rows:
                context = event.get("context") if isinstance(event.get("context"), Mapping) else {}
                project_id = str(event.get("project_id") or "unknown")
                root_path = str(context.get("root_path") or context.get("cwd") or "") or None
                repo_id = str(context.get("repo_id") or "") or None
                scope_key = (project_id, root_path or "", repo_id or "")
                if scope_key in seen_scopes:
                    continue
                seen_scopes.add(scope_key)
                self.resolve_project(
                    project_id=project_id,
                    root_path=root_path,
                    repo_id=repo_id,
                    persist=True,
                )
        return evaluations

    def _events_for_candidate_ids(
        self, candidates: Iterable[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        ids = sorted(
            {
                str(event_id)
                for candidate in candidates
                for event_id in (candidate.get("evidence_event_ids") or [])
                if str(event_id)
            }
        )
        if not ids:
            return {}
        with self.repository.db.connection() as conn:
            result: dict[str, dict[str, Any]] = {}
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM events WHERE event_id IN ({placeholders})", chunk
                ).fetchall()
                result.update({str(row["event_id"]): self._event_from_row(row) for row in rows})
            return result

    def review_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        events: Mapping[str, Mapping[str, Any]] | None = None,
        persist: bool = True,
    ) -> CandidateQualityReview:
        event_map = events or self._events_for_candidate_ids([candidate])
        evaluations: dict[str, EventQualityEvaluation] = {}
        existing_map = self.store.get_event_evaluations(event_map.keys())
        for event_id, event in event_map.items():
            existing = existing_map.get(event_id)
            if existing and existing.get("classifier_version") == self.classifier_version:
                evaluations[event_id] = self._evaluation_from_dict(existing)
            else:
                evaluations[event_id] = classify_event(
                    event, classifier_version=self.classifier_version
                )
        review = review_candidate(
            candidate,
            event_map,
            event_evaluations=evaluations,
            classifier_version=self.classifier_version,
        )
        if persist:
            self.store.upsert_candidate_review(review)
        return review

    def review_candidates(
        self,
        candidates: Iterable[Mapping[str, Any]],
        *,
        persist: bool = True,
    ) -> list[CandidateQualityReview]:
        rows = list(candidates)
        if not rows:
            return []
        event_map = self._events_for_candidate_ids(rows)
        # Ensure every cited event has a current evaluation before candidate
        # review.  This also repairs stores imported before Phase 4A.
        event_evaluations: dict[str, EventQualityEvaluation] = {}
        missing: list[Mapping[str, Any]] = []
        existing_map = self.store.get_event_evaluations(event_map.keys())
        for event_id, event in event_map.items():
            existing = existing_map.get(event_id)
            if existing and existing.get("classifier_version") == self.classifier_version:
                event_evaluations[event_id] = self._evaluation_from_dict(existing)
            else:
                missing.append(event)
        if missing:
            fresh = self.evaluate_events(missing, persist=persist)
            event_evaluations.update({item.event_id: item for item in fresh})
        reviews = [
            review_candidate(
                candidate,
                event_map,
                event_evaluations=event_evaluations,
                classifier_version=self.classifier_version,
            )
            for candidate in rows
        ]
        if persist:
            self.store.upsert_candidate_reviews(reviews)
        return reviews

    def _selected_event_rows(
        self,
        *,
        project_id: str | None,
        task_id: str | None,
        logical_project_id_value: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("e.project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append("e.task_id=?")
            params.append(task_id)
        if logical_project_id_value:
            raw_ids = self.store.raw_project_ids_for_logical(logical_project_id_value)
            if raw_ids:
                placeholders = ",".join("?" for _ in raw_ids)
                clauses.append(f"e.project_id IN ({placeholders})")
                params.extend(raw_ids)
            else:
                clauses.append("1=0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.repository.db.connection() as conn:
            rows = conn.execute(
                f"SELECT e.* FROM events e {where} ORDER BY e.occurred_at,e.seq,e.event_id LIMIT ?",
                (*params, max(1, min(int(limit), 1_000_000))),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def _selected_candidates(
        self,
        *,
        project_id: str | None,
        task_id: str | None,
        logical_project_id_value: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("c.project_id=?")
            params.append(project_id)
        if task_id:
            clauses.append("c.task_id=?")
            params.append(task_id)
        if logical_project_id_value:
            raw_ids = self.store.raw_project_ids_for_logical(logical_project_id_value)
            if raw_ids:
                placeholders = ",".join("?" for _ in raw_ids)
                clauses.append(f"c.project_id IN ({placeholders})")
                params.extend(raw_ids)
            else:
                clauses.append("1=0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.repository.db.connection() as conn:
            rows = conn.execute(
                f"SELECT c.* FROM memory_candidates c {where} ORDER BY c.created_at,c.candidate_id LIMIT ?",
                (*params, max(1, min(int(limit), 1_000_000))),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                result.append(
                    {
                        "candidate_id": str(row["candidate_id"]),
                        "project_id": str(row["project_id"]),
                        "task_id": str(row["task_id"]),
                        "kind": str(row["kind"]),
                        "statement": str(row["statement"]),
                        "aliases": json_value(row["aliases_json"], []),
                        "bindings": json_value(row["bindings_json"], []),
                        "evidence_event_ids": json_value(row["evidence_event_ids_json"], []),
                        "confidence": float(row["confidence"]),
                        "uncertainty": str(row["uncertainty"]),
                    }
                )
            return result

    def replay(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        logical_project_id_value: str | None = None,
        limit: int = 100_000,
        write: bool = True,
    ) -> QualityRunResult:
        events = self._selected_event_rows(
            project_id=project_id,
            task_id=task_id,
            logical_project_id_value=logical_project_id_value,
            limit=limit,
        )
        candidates = self._selected_candidates(
            project_id=project_id,
            task_id=task_id,
            logical_project_id_value=logical_project_id_value,
            limit=limit,
        )
        canonical = {
            "classifier_version": self.classifier_version,
            "events": [str(event["event_id"]) for event in events],
            "candidates": [str(candidate["candidate_id"]) for candidate in candidates],
            "filters": {
                "project_id": project_id,
                "task_id": task_id,
                "logical_project_id": logical_project_id_value,
            },
        }
        input_hash = hashlib.sha256(json_text(canonical).encode("utf-8")).hexdigest()
        scope = (
            "task"
            if task_id
            else "project"
            if project_id
            else "logical_project"
            if logical_project_id_value
            else "all"
        )
        mode = "write" if write else "dry_run"
        run_id = self.store.start_run(
            scope=scope,
            input_hash=input_hash,
            classifier_version=self.classifier_version,
            mode=mode,
            project_id=project_id,
            task_id=task_id,
            logical_project_id_value=logical_project_id_value,
        )
        try:
            event_evaluations = self.evaluate_events(events, persist=write)
            reviews = self.review_candidates(candidates, persist=write)
            # ``evaluate_events`` already persists every selected event and
            # resolves each unique project/root/repository scope in one small
            # batch.  Do not re-open a SQLite transaction per event here: a
            # large history replay can otherwise turn a 20-second operation
            # into an hours-long tail while producing no new information.
            event_counts = Counter(item.decision for item in event_evaluations)
            candidate_counts = Counter(item.decision for item in reviews)
            logical_ids: set[str] = set()
            seen_scopes: set[tuple[str, str, str]] = set()
            for event in events:
                context = event.get("context") if isinstance(event.get("context"), Mapping) else {}
                project = str(event.get("project_id") or "unknown")
                root = str(context.get("root_path") or context.get("cwd") or "") or None
                repo = str(context.get("repo_id") or "") or None
                scope_key = (project, root or "", repo or "")
                if scope_key in seen_scopes:
                    continue
                seen_scopes.add(scope_key)
                logical_ids.add(
                    self.resolve_project(
                        project_id=project,
                        root_path=root,
                        repo_id=repo,
                        persist=False,
                    )["logical_project_id"]
                )
            counts = {
                "events": len(events),
                "candidates": len(candidates),
                "event_decisions": dict(event_counts),
                "candidate_decisions": dict(candidate_counts),
                "logical_projects": len(logical_ids),
                "alias_suggestions": self._alias_suggestion_count(events),
            }
            self.store.finish_run(run_id, status="succeeded", counts=counts)
            return QualityRunResult(
                run_id=run_id,
                scope=scope,
                mode=mode,
                status="succeeded",
                event_count=len(events),
                candidate_count=len(candidates),
                logical_project_count=len(logical_ids),
                event_decisions=dict(event_counts),
                candidate_decisions=dict(candidate_counts),
                alias_suggestions=int(counts["alias_suggestions"]),
                input_hash=input_hash,
            )
        except Exception as exc:
            self.store.finish_run(
                run_id, status="failed", counts={}, error=f"{type(exc).__name__}: {exc}"
            )
            raise

    @staticmethod
    def _alias_suggestion_count(events: Iterable[Mapping[str, Any]]) -> int:
        roots: dict[str, set[str]] = {}
        for event in events:
            context = event.get("context") if isinstance(event.get("context"), Mapping) else {}
            root = normalize_root(context.get("root_path") or context.get("cwd"))
            if root:
                roots.setdefault(root_basename(root), set()).add(root)
        # A basename with multiple roots is an explicit review opportunity,
        # not an automatic merge.  Project_J checkouts are the common case.
        return sum(1 for values in roots.values() if len(values) > 1)

    def report(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        logical_project_id_value: str | None = None,
    ) -> dict[str, Any]:
        result = self.store.report(
            project_id=project_id,
            task_id=task_id,
            logical_project_id_value=logical_project_id_value,
            classifier_version=self.classifier_version,
        )
        result["classifier_version"] = self.classifier_version
        result["latest_runs"] = self.store.latest_runs(limit=10)
        return result

    def candidate_quality(self, candidate_id: str) -> dict[str, Any] | None:
        return self.store.get_candidate_review(candidate_id)

    def event_quality(self, event_id: str) -> dict[str, Any] | None:
        return self.store.get_event_evaluation(event_id)

    def register_project_alias(
        self,
        *,
        raw_project_id: str,
        logical_project_id: str,
        alias_value: str,
        alias_type: str = "explicit",
        normalized_root: str = "",
        confidence: float = 1.0,
        evidence: Mapping[str, Any] | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        return self.store.register_alias(
            raw_project_id=raw_project_id,
            logical_project_id_value=logical_project_id,
            alias_value=alias_value,
            alias_type=alias_type,
            normalized_root=normalize_root(normalized_root),
            confidence=confidence,
            evidence=evidence,
            display_name=display_name,
        )
