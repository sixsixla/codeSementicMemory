"""Safe maintenance operations for rebuildable memory projections."""

from __future__ import annotations

from typing import Any

from .storage.database import Database


class ProjectionMaintenance:
    """Reset derived extraction/card projections without touching source facts.

    Canonical projects, tasks, sessions, events, artifacts, and the durable
    outbox are intentionally outside this operation. Candidate, card, and
    quality tables are rebuildable projections, so clearing them is useful
    after an extractor/sanitizer/quality-policy upgrade. The caller must opt
    in explicitly; the CLI exposes this as ``rebuild-memory --yes``.
    """

    _TABLES = (
        # Children first to satisfy the database's foreign-key contract.
        "candidate_quality_reviews",
        "event_quality_evaluations",
        "project_aliases",
        "quality_runs",
        "logical_projects",
        "memory_card_fts",
        "memory_card_links",
        "memory_card_bindings",
        "memory_card_evidence",
        "memory_card_lifecycle_events",
        "consolidation_decisions",
        "memory_card_versions",
        "memory_cards",
        "memory_candidate_fts",
        "memory_candidate_links",
        "memory_candidate_evidence",
        "memory_candidates",
        "extraction_runs",
    )

    def __init__(self, database: Database):
        self.db = database
        self.db.ensure_initialized()

    def reset(self) -> dict[str, Any]:
        """Delete only rebuildable memory projections in one transaction."""

        deleted: dict[str, int] = {}
        with self.db.transaction() as conn:
            for table in self._TABLES:
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                conn.execute(f"DELETE FROM {table}")
                deleted[table] = count
        return {"deleted": deleted, "source_tables_preserved": True}
