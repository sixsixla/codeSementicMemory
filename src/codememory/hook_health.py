"""End-to-end health reporting for the local Codex hook memory loop."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .hook_support import HOOK_ERROR_PREFIX, resolve_hook_project
from .storage.database import Database


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _latest(conn: Any, table: str, column: str) -> str | None:
    row = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _structured_errors(path: Path, *, since: datetime, limit: int = 20) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "present": False,
            "size_bytes": 0,
            "recent_count": 0,
            "by_type": {},
            "latest": [],
        }
    max_bytes = 4_000_000
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - max_bytes))
        text = handle.read().decode("utf-8", errors="replace")
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith(HOOK_ERROR_PREFIX):
            continue
        try:
            record = json.loads(line[len(HOOK_ERROR_PREFIX) :])
            timestamp = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp < since:
            continue
        record.pop("traceback", None)
        records.append(record)
    records.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return {
        "path": str(path),
        "present": True,
        "size_bytes": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "recent_count": len(records),
        "by_type": dict(Counter(str(item.get("error_type") or "unknown") for item in records)),
        "latest": records[: max(1, min(int(limit), 100))],
    }


class HookHealthService:
    def __init__(self, database: Database, *, repo_root: Path) -> None:
        self.database = database
        self.repo_root = repo_root
        self.database.ensure_initialized()

    def report(
        self,
        *,
        hours: int = 24,
        stale_minutes: int = 30,
        hook_log: Path | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=max(1, min(int(hours), 24 * 90)))
        stale_before = now - timedelta(minutes=max(1, int(stale_minutes)))
        log_path = hook_log or Path(self.database.path).with_name("codememory-hook.log")
        with self.database.connection() as conn:
            hook_rows = conn.execute(
                "SELECT COALESCE(json_extract(context_json,'$.hook_event_name'),'<unknown>') AS hook_name,"
                "event_type,project_id,COUNT(*) AS n,MIN(occurred_at) AS first_at,"
                "MAX(occurred_at) AS last_at FROM events "
                "WHERE producer_adapter='codex-hook' AND occurred_at>=? "
                "GROUP BY hook_name,event_type,project_id ORDER BY last_at DESC",
                (_iso(since),),
            ).fetchall()
            hook_total = sum(int(row["n"]) for row in hook_rows)
            by_hook = Counter()
            by_project = Counter()
            for row in hook_rows:
                by_hook[str(row["hook_name"])] += int(row["n"])
                by_project[str(row["project_id"])] += int(row["n"])

            scope_rows = conn.execute(
                "SELECT project_id,json_extract(context_json,'$.cwd') AS cwd,COUNT(*) AS n,"
                "MAX(occurred_at) AS last_at FROM events "
                "WHERE producer_adapter='codex-hook' AND occurred_at>=? "
                "GROUP BY project_id,cwd ORDER BY last_at DESC",
                (_iso(since),),
            ).fetchall()
            mappings: list[dict[str, Any]] = []
            for row in scope_rows:
                cwd = str(row["cwd"] or "")
                expected = resolve_hook_project({"cwd": cwd}, repo_root=self.repo_root)
                mappings.append(
                    {
                        "cwd": cwd,
                        "stored_project_id": str(row["project_id"]),
                        "expected_project_id": expected,
                        "matches_current_rules": str(row["project_id"]) == expected,
                        "events": int(row["n"]),
                        "last_at": row["last_at"],
                    }
                )

            maintenance_rows = conn.execute(
                "SELECT status,COUNT(*) AS n,MAX(updated_at) AS latest FROM agent_memory_maintenance "
                "WHERE updated_at>=? GROUP BY status",
                (_iso(since),),
            ).fetchall()
            maintenance_counts = {
                "pending": 0,
                "completed": 0,
                "failed": 0,
                "superseded": 0,
            }
            maintenance_latest: dict[str, str | None] = {}
            for row in maintenance_rows:
                maintenance_counts[str(row["status"])] = int(row["n"])
                maintenance_latest[str(row["status"])] = row["latest"]
            stale_pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM agent_memory_maintenance "
                    "WHERE status='pending' AND updated_at<?",
                    (_iso(stale_before),),
                ).fetchone()[0]
            )
            latest_maintenance = conn.execute(
                "SELECT maintenance_id,project_id,task_id,session_id,status,attempts,max_attempts,"
                "input_hash,model,note_count,candidate_count,last_error,requested_at,updated_at,"
                "completed_at FROM agent_memory_maintenance ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()

            extraction_recent = int(
                conn.execute(
                    "SELECT COUNT(*) FROM extraction_runs WHERE created_at>=?",
                    (_iso(since),),
                ).fetchone()[0]
            )
            agent_extraction_recent = int(
                conn.execute(
                    "SELECT COUNT(*) FROM extraction_runs "
                    "WHERE created_at>=? AND provider='agent-llm'",
                    (_iso(since),),
                ).fetchone()[0]
            )
            candidates_recent = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_candidates WHERE created_at>=?",
                    (_iso(since),),
                ).fetchone()[0]
            )
            cards_recent = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_cards WHERE updated_at>=?",
                    (_iso(since),),
                ).fetchone()[0]
            )
            hook_outbox = {
                str(row["status"]): int(row["n"])
                for row in conn.execute(
                    "SELECT o.status,COUNT(*) AS n FROM outbox o "
                    "JOIN events e ON e.event_id=o.aggregate_id "
                    "WHERE e.producer_adapter='codex-hook' AND e.occurred_at>=? "
                    "GROUP BY o.status",
                    (_iso(since),),
                ).fetchall()
            }
            cycle_status = {
                str(row["status"]): int(row["n"])
                for row in conn.execute(
                    "SELECT status,COUNT(*) AS n FROM agent_memory_cycles "
                    "WHERE updated_at>=? GROUP BY status",
                    (_iso(since),),
                ).fetchall()
            }
            freshness = {
                "latest_hook_event": conn.execute(
                    "SELECT MAX(occurred_at) FROM events WHERE producer_adapter='codex-hook'"
                ).fetchone()[0],
                "latest_extraction": _latest(conn, "extraction_runs", "created_at"),
                "latest_candidate": _latest(conn, "memory_candidates", "created_at"),
                "latest_card_update": _latest(conn, "memory_cards", "updated_at"),
            }

        errors = _structured_errors(log_path, since=since)
        issues: list[dict[str, str]] = []
        if hook_total == 0:
            issues.append(
                {
                    "severity": "warning",
                    "code": "no_recent_hook_events",
                    "message": "No Codex hook events were recorded in the selected window.",
                }
            )
        if maintenance_counts["failed"]:
            issues.append(
                {
                    "severity": "warning",
                    "code": "maintenance_failed",
                    "message": f"{maintenance_counts['failed']} maintenance requests failed in the selected window.",
                }
            )
        if stale_pending:
            issues.append(
                {
                    "severity": "warning",
                    "code": "maintenance_stale",
                    "message": f"{stale_pending} maintenance requests are still pending past the stale threshold.",
                }
            )
        if by_hook.get("Stop", 0) and maintenance_counts["completed"] == 0:
            issues.append(
                {
                    "severity": "warning",
                    "code": "stop_without_completed_maintenance",
                    "message": "Stop hooks ran, but no current-agent maintenance completed in the selected window.",
                }
            )
        if hook_total and extraction_recent == 0:
            issues.append(
                {
                    "severity": "warning",
                    "code": "memory_projection_not_fresh",
                    "message": "Hook events are fresh, but extraction has not run in the selected window.",
                }
            )
        mismatched = sum(item["events"] for item in mappings if not item["matches_current_rules"])
        if mismatched:
            issues.append(
                {
                    "severity": "warning",
                    "code": "legacy_project_fragmentation",
                    "message": f"{mismatched} recent events use project IDs that differ from the current root rules.",
                }
            )
        if errors["recent_count"]:
            issues.append(
                {
                    "severity": "warning",
                    "code": "hook_fail_open_errors",
                    "message": f"{errors['recent_count']} structured fail-open hook errors were logged.",
                }
            )
        status = "ok" if not issues else "degraded"
        return {
            "status": status,
            "generated_at": _iso(now),
            "window": {"hours": hours, "since": _iso(since)},
            "database": str(Path(self.database.path).resolve()),
            "hooks": {
                "total_events": hook_total,
                "by_hook": dict(by_hook),
                "by_project": dict(by_project),
                "cycles": cycle_status,
                "rows": [dict(row) for row in hook_rows],
            },
            "project_mapping": {
                "mismatched_event_count": mismatched,
                "scopes": mappings,
            },
            "maintenance": {
                "counts": maintenance_counts,
                "latest_by_status": maintenance_latest,
                "stale_pending": stale_pending,
                "recent": [dict(row) for row in latest_maintenance],
            },
            "memory_projection": {
                "extractions": extraction_recent,
                "agent_llm_extractions": agent_extraction_recent,
                "candidates": candidates_recent,
                "cards_updated": cards_recent,
                "freshness": freshness,
            },
            "outbox": {"hook_events": hook_outbox},
            "errors": errors,
            "issues": issues,
        }
