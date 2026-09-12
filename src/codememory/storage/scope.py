"""Logical project-scope helpers shared by read-side projections."""

from __future__ import annotations

from typing import Any

from .database import Database


_ALIAS_PRIORITY_SQL = (
    "CASE alias_type WHEN 'explicit' THEN 0 WHEN 'repo' THEN 1 "
    "WHEN 'basename' THEN 2 WHEN 'root' THEN 3 ELSE 4 END"
)


def raw_project_ids_for_logical(database: Database, logical_project_id: str | None) -> list[str]:
    """Return raw project IDs whose strongest alias points to one logical scope.

    Alias rows are deliberately append-only.  A raw ID may have historical
    heuristic aliases to several logical scopes; priority and recency select
    one effective owner while keeping every old row auditable.
    """

    logical_id = str(logical_project_id or "").strip()
    if not logical_id:
        return []
    with database.connection() as conn:
        rows = conn.execute(
            "SELECT raw_project_id,logical_project_id FROM ("
            "SELECT raw_project_id,logical_project_id,ROW_NUMBER() OVER ("
            "PARTITION BY raw_project_id ORDER BY "
            + _ALIAS_PRIORITY_SQL
            + ",confidence DESC,updated_at DESC,alias_id"
            ") AS rn FROM project_aliases) WHERE rn=1 AND logical_project_id=? "
            "ORDER BY raw_project_id",
            (logical_id,),
        ).fetchall()
        return [str(row["raw_project_id"]) for row in rows]


def project_scope_clause(
    database: Database,
    *,
    alias: str,
    logical_project_id: str | None,
    params: list[Any],
) -> str:
    """Build a SQL predicate for a logical scope and append its parameters."""

    logical_id = str(logical_project_id or "").strip()
    if not logical_id:
        return ""
    raw_ids = raw_project_ids_for_logical(database, logical_id)
    if not raw_ids:
        return "1=0"
    placeholders = ",".join("?" for _ in raw_ids)
    params.extend(raw_ids)
    return f"{alias}.project_id IN ({placeholders})"


def effective_logical_project_id(database: Database, raw_project_id: str | None) -> str | None:
    """Return the highest-priority logical owner for one raw project ID."""

    raw_id = str(raw_project_id or "").strip()
    if not raw_id:
        return None
    with database.connection() as conn:
        row = conn.execute(
            "SELECT logical_project_id FROM project_aliases WHERE raw_project_id=? "
            "ORDER BY " + _ALIAS_PRIORITY_SQL + ",confidence DESC,updated_at DESC,alias_id LIMIT 1",
            (raw_id,),
        ).fetchone()
    return str(row["logical_project_id"]) if row else None


def logical_scope_map(database: Database) -> dict[str, str]:
    """Return effective raw-to-logical aliases for diagnostics."""

    with database.connection() as conn:
        rows = conn.execute(
            "SELECT raw_project_id,logical_project_id FROM ("
            "SELECT raw_project_id,logical_project_id,ROW_NUMBER() OVER ("
            "PARTITION BY raw_project_id ORDER BY "
            + _ALIAS_PRIORITY_SQL
            + ",confidence DESC,updated_at DESC,alias_id"
            ") AS rn FROM project_aliases) WHERE rn=1"
        ).fetchall()
    return {str(row["raw_project_id"]): str(row["logical_project_id"]) for row in rows}
