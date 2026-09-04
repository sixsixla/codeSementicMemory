"""Command line interface for local development and replay."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .api.app import create_app
from .adapters import CliProducer
from .config import default_db_path, event_schema_path
from .consolidation.service import ConsolidationService
from .consolidation.store import CardStore
from .domain.events import EventEnvelope, EventType
from .extraction.service import ExtractionService
from .extraction.store import ExtractionStore
from .extraction.providers import provider_from_name
from .history.codex import CodexHistoryImporter
from .ingest.service import IngestService, replay_jsonl
from .maintenance import ProjectionMaintenance
from .quality.service import QualityService
from .storage.database import Database
from .storage.repository import ConflictError, MemoryRepository
from .verification.service import VerificationService
from .workers.extraction import ExtractionWorker


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db).expanduser() if getattr(args, "db", None) else default_db_path()


def _service(path: Path) -> tuple[Database, MemoryRepository, IngestService]:
    database = Database(path)
    repository = MemoryRepository(database)
    return database, repository, IngestService(repository)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codememory", description="Local CodeSementicMemory tools"
    )
    parser.add_argument("--version", action="version", version="0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize a SQLite database")
    init.add_argument("--db", help="database path")

    health = sub.add_parser("health", help="show database health")
    health.add_argument("--db", help="database path")
    health.add_argument(
        "--deep",
        action="store_true",
        help="run the exhaustive SQLite integrity check (slow on large archives)",
    )

    doctor = sub.add_parser("doctor", help="run local installation and schema checks")
    doctor.add_argument("--db", help="database path")

    schema = sub.add_parser("schema", help="validate event schema inputs")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    schema_validate = schema_sub.add_parser("validate", help="validate a JSON or JSONL file")
    schema_validate.add_argument("path", type=Path)

    backup = sub.add_parser("backup", help="create a consistent SQLite backup")
    backup.add_argument("output", type=Path)
    backup.add_argument("--db", help="database path")

    restore = sub.add_parser("restore", help="restore a SQLite backup into a database")
    restore.add_argument("source", type=Path)
    restore.add_argument("--db", required=True, help="destination database path")
    restore.add_argument("--force", action="store_true", help="replace an existing destination")

    export = sub.add_parser("export", help="export canonical events as JSON or JSONL")
    export.add_argument("output", type=Path)
    export.add_argument("--db", help="database path")
    export.add_argument("--task-id")
    export.add_argument("--project-id")
    export.add_argument("--format", choices=["jsonl", "json"], default="jsonl")

    replay = sub.add_parser("replay", help="replay one JSONL event fixture")
    replay.add_argument("path", type=Path)
    replay.add_argument("--db", help="database path")
    replay.add_argument(
        "--strict", action="store_true", help="stop on the first invalid/conflicting line"
    )

    emit = sub.add_parser("emit", help="emit one manually constructed coding event")
    emit.add_argument("event_type", choices=[item.value for item in EventType])
    emit.add_argument("--project-id", required=True)
    emit.add_argument("--task-id", required=True)
    emit.add_argument("--session-id")
    emit.add_argument("--seq", type=int, default=0)
    emit.add_argument("--text")
    emit.add_argument("--payload-json", help="JSON object merged into the event payload")
    emit.add_argument("--db", help="database path")

    timeline = sub.add_parser("timeline", help="print a task event timeline")
    timeline.add_argument("task_id")
    timeline.add_argument("--session-id")
    timeline.add_argument("--limit", type=int, default=200)
    timeline.add_argument("--db", help="database path")

    search = sub.add_parser("search", help="search the local FTS projection")
    search.add_argument("query")
    search.add_argument("--project-id")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--db", help="database path")

    rebuild = sub.add_parser("rebuild-search", help="rebuild the optional FTS projection")
    rebuild.add_argument("--db", help="database path")

    rebuild_memory = sub.add_parser(
        "rebuild-memory",
        help="clear rebuildable candidate/card/quality projections after a policy upgrade",
    )
    rebuild_memory.add_argument("--db", help="database path")
    rebuild_memory.add_argument(
        "--yes",
        action="store_true",
        help="confirm deleting only extraction/card/quality projections (source events stay intact)",
    )

    outbox = sub.add_parser("outbox", help="inspect or claim outbox jobs")
    outbox.add_argument("--db", help="database path")
    outbox.add_argument("--status")
    outbox.add_argument("--limit", type=int, default=100)
    outbox.add_argument("--claim", action="store_true")
    outbox.add_argument("--lease-seconds", type=int, default=60)

    import_history = sub.add_parser(
        "import-codex-history", help="ingest a bounded Codex read_thread/history JSON export"
    )
    import_history.add_argument("path", type=Path)
    import_history.add_argument("--db", help="database path")
    import_history.add_argument("--project-id", help="override the derived project id")
    import_history.add_argument(
        "--atomic", action="store_true", help="roll back the complete import on one invalid event"
    )
    import_history.add_argument("--batch-size", type=int, default=250)
    import_history.add_argument("--max-threads", type=int)
    import_history.add_argument("--since", help="only import threads updated at/after ISO timestamp")
    import_history.add_argument("--extract", action="store_true", help="run extraction once per imported task")
    import_history.add_argument("--consolidate", action="store_true", help="consolidate candidates after extraction/import")
    import_history.add_argument(
        "--provider",
        choices=["mock", "openai-compatible", "local"],
        default="mock",
        help="provider used with --extract",
    )
    import_history.add_argument(
        "--summary-only",
        action="store_true",
        help="omit per-task extraction details from the JSON output",
    )

    extract = sub.add_parser("extract", help="extract candidate memories for one task")
    extract.add_argument("task_id")
    extract.add_argument("--db", help="database path")
    extract.add_argument("--session-id")
    extract.add_argument("--provider", choices=["mock", "openai-compatible", "local"], default="mock")
    extract.add_argument("--force", action="store_true", help="rerun an idempotent input window")

    extract_all = sub.add_parser("extract-all", help="extract each canonical task once")
    extract_all.add_argument("--db", help="database path")
    extract_all.add_argument("--project-id")
    extract_all.add_argument("--limit", type=int, default=100_000)
    extract_all.add_argument(
        "--since", help="only extract tasks whose canonical activity is at/after ISO timestamp"
    )
    extract_all.add_argument(
        "--min-events", type=int, default=0, help="skip tasks with fewer canonical events"
    )
    extract_all.add_argument("--provider", choices=["mock", "openai-compatible", "local"], default="mock")
    extract_all.add_argument("--consolidate", action="store_true")
    extract_all.add_argument("--complete-outbox", action="store_true")
    extract_all.add_argument(
        "--summary-only", action="store_true", help="omit per-task extraction details from the JSON output"
    )

    extract_outbox = sub.add_parser("extract-outbox", help="claim event.ingested jobs and extract once")
    extract_outbox.add_argument("--db", help="database path")
    extract_outbox.add_argument("--limit", type=int, default=20)
    extract_outbox.add_argument("--lease-seconds", type=int, default=120)

    memories = sub.add_parser("memories", help="list candidate memories")
    memories.add_argument("--db", help="database path")
    memories.add_argument("--task-id")
    memories.add_argument("--kind")
    memories.add_argument("--limit", type=int, default=100)
    memories.add_argument(
        "--include-quarantine",
        action="store_true",
        help="include quarantined candidate memories for audit",
    )

    graph = sub.add_parser("graph", help="print the graph projection used by the web UI")
    graph.add_argument("--db", help="database path")
    graph.add_argument("--task-id")
    graph.add_argument("--limit", type=int, default=300)
    graph.add_argument(
        "--include-quarantine",
        action="store_true",
        help="include quarantined candidate/card nodes for audit",
    )

    quality_report = sub.add_parser(
        "quality-report", help="show continuous quality-gate coverage and decisions"
    )
    quality_report.add_argument("--db", help="database path")
    quality_report.add_argument("--project-id")
    quality_report.add_argument("--task-id")
    quality_report.add_argument("--logical-project-id")

    quality_replay = sub.add_parser(
        "quality-replay", help="re-evaluate events/candidates with the versioned quality gate"
    )
    quality_replay.add_argument("--db", help="database path")
    quality_replay.add_argument("--project-id")
    quality_replay.add_argument("--task-id")
    quality_replay.add_argument("--logical-project-id")
    quality_replay.add_argument("--limit", type=int, default=100_000)
    quality_replay.add_argument(
        "--write",
        action="store_true",
        help="persist evaluations and aliases (default previews them; the replay audit is still recorded)",
    )

    verify = sub.add_parser(
        "verify-bindings",
        aliases=["verify-project-j"],
        help="verify current Project_J card bindings against P4/CodeBaseMemory manifests",
    )
    verify.add_argument("--db", help="database path")
    verify.add_argument("--logical-project-id")
    verify.add_argument("--task-id", action="append", help="limit verification to one or more task ids")
    verify.add_argument(
        "--manifest",
        action="append",
        type=Path,
        help="portable verification manifest (repeatable; provider is read from JSON)",
    )
    verify.add_argument("--p4-manifest", type=Path)
    verify.add_argument("--codebase-memory-manifest", type=Path)
    verify.add_argument(
        "--p4-path",
        action="append",
        help="explicit depot/client path for a read-only p4 fstat collection",
    )
    verify.add_argument("--p4-root")
    verify.add_argument("--p4-port")
    verify.add_argument("--p4-user")
    verify.add_argument("--p4-client")
    verify.add_argument("--p4-executable", default="p4")
    verify.add_argument("--p4-timeout", type=int, default=30)
    verify.add_argument("--limit", type=int, default=100_000)
    verify.add_argument("--write", action="store_true")
    verify.add_argument("--include-quarantine", action="store_true")

    verification_report = sub.add_parser(
        "verification-report", help="show Project_J binding-verification coverage and runs"
    )
    verification_report.add_argument("--db", help="database path")
    verification_report.add_argument("--logical-project-id")
    verification_report.add_argument("--limit", type=int, default=20)
    verification_report.add_argument(
        "--include-manifest",
        action="store_true",
        help="include complete normalized snapshot manifests (keep the limit small)",
    )

    quality_projects = sub.add_parser(
        "quality-projects", help="list logical projects and auditable raw-id aliases"
    )
    quality_projects.add_argument("--db", help="database path")
    quality_projects.add_argument("--limit", type=int, default=1000)

    project_alias = sub.add_parser(
        "project-alias", help="attach a reviewed raw project id to a logical project"
    )
    project_alias.add_argument("--db", help="database path")
    project_alias.add_argument("--raw-project-id", required=True)
    project_alias.add_argument("--logical-project-id", required=True)
    project_alias.add_argument("--alias-value", required=True)
    project_alias.add_argument(
        "--alias-type",
        choices=["root", "repo", "basename", "explicit", "inferred"],
        default="explicit",
    )
    project_alias.add_argument("--normalized-root", default="")
    project_alias.add_argument("--confidence", type=float, default=1.0)
    project_alias.add_argument("--evidence-json", default="{}")
    project_alias.add_argument("--display-name")

    consolidate = sub.add_parser("consolidate", help="promote candidate memories into versioned cards")
    consolidate.add_argument("--db", help="database path")
    consolidate.add_argument("--project-id")
    consolidate.add_argument("--task-id")
    consolidate.add_argument("--candidate-id", action="append", dest="candidate_ids")
    consolidate.add_argument("--limit", type=int, default=1000)

    cards = sub.add_parser("cards", help="list versioned memory cards")
    cards.add_argument("--db", help="database path")
    cards.add_argument("--project-id")
    cards.add_argument("--task-id")
    cards.add_argument("--status")
    cards.add_argument("--kind")
    cards.add_argument("--limit", type=int, default=100)
    cards.add_argument(
        "--include-quarantine",
        action="store_true",
        help="include cards backed by quarantined candidates for audit",
    )

    card = sub.add_parser("card", help="inspect or transition one memory card")
    card_sub = card.add_subparsers(dest="card_command", required=True)
    for name in ("show", "history", "relations"):
        card_view = card_sub.add_parser(name)
        card_view.add_argument("card_id")
        card_view.add_argument("--db", help="database path")
    card_transition = card_sub.add_parser("transition")
    card_transition.add_argument("card_id")
    card_transition.add_argument("status")
    card_transition.add_argument("--reason", required=True)
    card_transition.add_argument("--actor", default="cli")
    card_transition.add_argument("--db", help="database path")
    card_promote = card_sub.add_parser("promote")
    card_promote.add_argument("card_id")
    card_promote.add_argument("--reason", required=True)
    card_promote.add_argument("--actor", default="cli")
    card_promote.add_argument("--db", help="database path")

    serve = sub.add_parser("serve", help="run the localhost HTTP API")
    serve.add_argument("--db", help="database path")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    return parser


def _print(value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    # Windows Codex terminals may expose a GBK stdout stream.  Verification
    # results can contain Unicode symbols from source paths or card text; make
    # the fallback explicit so a successful run is never reported as failed
    # merely while serializing its result.
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding:
        try:
            rendered.encode(encoding)
        except UnicodeEncodeError:
            rendered = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2)
    print(rendered)


def _load_json_object(path: Path) -> Any:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, (dict, list)):
        raise ValueError(f"manifest must be a JSON object or list: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = _db_path(args)
    try:
        if args.command == "init":
            _, repository, _ = _service(path)
            _print(repository.health())
            return 0
        if args.command == "health":
            _, repository, _ = _service(path)
            _print(repository.health(check_integrity=args.deep))
            return 0
        if args.command == "doctor":
            _, repository, _ = _service(path)
            health = repository.health()
            schema_path = event_schema_path()
            with repository.db.connection() as conn:
                fts = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_fts'"
                ).fetchone()
            result = {
                "status": "ok"
                if health["status"] == "ok" and schema_path.exists() and fts
                else "degraded",
                "health": health,
                "event_schema": str(schema_path),
                "event_schema_present": schema_path.exists(),
                "fts5_present": bool(fts),
            }
            _print(result)
            return 0 if result["status"] == "ok" else 1
        if args.command == "schema":
            result = validate_schema_file(args.path)
            _print(result)
            return 0 if result["invalid"] == 0 else 2
        if args.command == "backup":
            database, _, _ = _service(path)
            _print({"backup": str(database.backup_to(args.output).resolve())})
            return 0
        if args.command == "restore":
            restored = Database.restore_from(args.source, path, force=args.force)
            _print({"restored": str(restored.resolve())})
            return 0
        if args.command == "export":
            _, repository, _ = _service(path)
            records = repository.export_events(task_id=args.task_id, project_id=args.project_id)
            if args.format == "json":
                args.output.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            else:
                args.output.write_text(
                    "".join(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                        for record in records
                    ),
                    encoding="utf-8",
                )
            _print(
                {
                    "output": str(args.output.resolve()),
                    "events": len(records),
                    "format": args.format,
                }
            )
            return 0
        if args.command == "replay":
            _, _, service = _service(path)
            summary = replay_jsonl(args.path, service, strict=args.strict)
            _print(summary.as_dict())
            return 0 if not summary.conflicts and not summary.invalid else 2
        if args.command == "emit":
            payload: dict[str, Any] = {}
            if args.payload_json:
                loaded = json.loads(args.payload_json)
                if not isinstance(loaded, dict):
                    raise ValueError("--payload-json must be a JSON object")
                payload.update(loaded)
            if args.text is not None:
                payload["text"] = args.text
            event = CliProducer().event(
                event_type=EventType(args.event_type),
                project_id=args.project_id,
                task_id=args.task_id,
                session_id=args.session_id,
                seq=args.seq,
                payload=payload,
            )
            _, _, service = _service(path)
            _print({"event": event.canonical_dict(), "result": service.ingest(event).as_dict()})
            return 0
        if args.command == "timeline":
            _, repository, _ = _service(path)
            _print(
                {
                    "task_id": args.task_id,
                    "session_id": args.session_id,
                    "events": repository.timeline(
                        args.task_id, session_id=args.session_id, limit=args.limit
                    ),
                }
            )
            return 0
        if args.command == "search":
            _, repository, _ = _service(path)
            _print(
                {
                    "query": args.query,
                    "results": repository.search(
                        args.query, project_id=args.project_id, limit=args.limit
                    ),
                }
            )
            return 0
        if args.command == "rebuild-search":
            _, repository, _ = _service(path)
            _print({"rebuilt_events": repository.rebuild_search_index()})
            return 0
        if args.command == "rebuild-memory":
            if not args.yes:
                raise ValueError("rebuild-memory is destructive to derived projections; pass --yes")
            database, _, _ = _service(path)
            _print(ProjectionMaintenance(database).reset())
            return 0
        if args.command == "outbox":
            _, repository, _ = _service(path)
            jobs = (
                repository.claim_outbox(limit=args.limit, lease_seconds=args.lease_seconds)
                if args.claim
                else repository.list_outbox(status=args.status, limit=args.limit)
            )
            _print({"counts": repository.outbox_count(), "jobs": [job.as_dict() for job in jobs]})
            return 0
        if args.command == "import-codex-history":
            database, repository, service = _service(path)
            since = None
            if args.since:
                since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
            summary = CodexHistoryImporter(service, project_id=args.project_id).import_file(
                args.path,
                atomic=args.atomic,
                batch_size=args.batch_size,
                max_threads=args.max_threads,
                since=since,
            )
            result = summary.as_dict()
            task_results: list[dict[str, Any]] = []
            if args.extract or args.consolidate:
                extraction_provider = provider_from_name(args.provider)
                extraction = ExtractionService(
                    repository,
                    store=ExtractionStore(database),
                    provider=extraction_provider,
                )
                cards = ConsolidationService(repository, store=CardStore(database))
                for task_id in summary.tasks:
                    extraction_result = None
                    consolidation_result = None
                    if args.extract:
                        extraction_result = extraction.extract_task(task_id).as_dict()
                    if args.consolidate:
                        consolidation_result = cards.consolidate(task_id=task_id).as_dict()
                    repository.complete_outbox_for_task(task_id)
                    if not args.summary_only:
                        task_results.append(
                            {
                                "task_id": task_id,
                                "extraction": extraction_result,
                                "consolidation": consolidation_result,
                            }
                        )
                result["processed_tasks"] = len(task_results)
                if args.summary_only:
                    result["processed_tasks"] = len(summary.tasks)
                else:
                    result["task_results"] = task_results
            _print(result)
            return 0 if summary.conflicts == 0 else 2
        if args.command == "extract":
            _, repository, _ = _service(path)
            extraction = ExtractionService(
                repository,
                store=ExtractionStore(repository.db),
                provider=provider_from_name(args.provider),
            )
            _print(
                extraction.extract_task(
                    args.task_id, session_id=args.session_id, force=args.force
                ).as_dict()
            )
            return 0
        if args.command == "extract-all":
            database, repository, _ = _service(path)
            extraction = ExtractionService(
                repository,
                store=ExtractionStore(database),
                provider=provider_from_name(args.provider),
            )
            cards = ConsolidationService(repository, store=CardStore(database))
            task_rows = repository.list_tasks(
                project_id=args.project_id,
                limit=args.limit,
                updated_since=args.since,
                min_events=args.min_events,
            )
            results: list[dict[str, Any]] = []
            aggregate = {
                "tasks": 0,
                "extracted": 0,
                "duplicates": 0,
                "failed": 0,
                "empty": 0,
                "candidates": 0,
                "cards_created": 0,
                "cards_merged": 0,
                "cards_new_versions": 0,
                "cards_uncertain": 0,
                "outbox_completed": 0,
            }
            total_tasks = len(task_rows)
            for index, task in enumerate(task_rows, start=1):
                task_id = task["task_id"]
                extracted = extraction.extract_task(task_id).as_dict()
                consolidated = cards.consolidate(task_id=task_id).as_dict() if args.consolidate else None
                completed_jobs = (
                    repository.complete_outbox_for_task(task_id) if args.complete_outbox else 0
                )
                aggregate["tasks"] += 1
                aggregate["extracted"] += int(extracted["status"] == "extracted")
                aggregate["duplicates"] += int(extracted["status"] == "duplicate")
                aggregate["failed"] += int(extracted["status"] in {"failed", "dead"})
                aggregate["empty"] += int(
                    extracted["status"] == "extracted" and extracted["candidate_count"] == 0
                )
                aggregate["candidates"] += int(extracted.get("candidate_count") or 0)
                aggregate["cards_created"] += int((consolidated or {}).get("created", 0))
                aggregate["cards_merged"] += int((consolidated or {}).get("merged", 0))
                aggregate["cards_new_versions"] += int((consolidated or {}).get("new_versions", 0))
                aggregate["cards_uncertain"] += int((consolidated or {}).get("uncertain", 0))
                aggregate["outbox_completed"] += completed_jobs
                if index == 1 or index % 25 == 0 or index == total_tasks:
                    print(
                        f"[extract-all] {index}/{total_tasks} tasks; candidates={aggregate['candidates']}",
                        file=sys.stderr,
                        flush=True,
                    )
                if not args.summary_only:
                    results.append(
                        {
                            "task_id": task_id,
                            "extraction": extracted,
                            "consolidation": consolidated,
                            "completed_outbox_jobs": completed_jobs,
                        }
                    )
            output = dict(aggregate)
            if not args.summary_only:
                output["results"] = results
            _print(output)
            return 0
        if args.command == "extract-outbox":
            _, repository, _ = _service(path)
            worker = ExtractionWorker(repository)
            _print(worker.run_once(limit=args.limit, lease_seconds=args.lease_seconds).as_dict())
            return 0
        if args.command == "memories":
            _, repository, _ = _service(path)
            store = ExtractionStore(repository.db)
            _print(
                {
                    "task_id": args.task_id,
                    "memories": store.list_candidates(
                        task_id=args.task_id,
                        kind=args.kind,
                        limit=args.limit,
                        include_quarantine=args.include_quarantine,
                    ),
                }
            )
            return 0
        if args.command == "consolidate":
            database, repository, _ = _service(path)
            result = ConsolidationService(repository, store=CardStore(database)).consolidate(
                project_id=args.project_id,
                task_id=args.task_id,
                candidate_ids=args.candidate_ids,
                limit=args.limit,
            )
            _print(result.as_dict())
            return 0
        if args.command == "cards":
            database, repository, _ = _service(path)
            _print(
                {
                    "cards": CardStore(database).list_cards(
                        project_id=args.project_id,
                        task_id=args.task_id,
                        status=args.status,
                        kind=args.kind,
                        limit=args.limit,
                        include_quarantine=args.include_quarantine,
                    )
                }
            )
            return 0
        if args.command == "card":
            database, repository, _ = _service(path)
            card_store = CardStore(database)
            card_service = ConsolidationService(repository, store=card_store)
            if args.card_command == "show":
                _print(card_store.get_card(args.card_id) or {"error": "card_not_found", "card_id": args.card_id})
                return 0
            if args.card_command == "history":
                _print({"card_id": args.card_id, "versions": card_store.card_history(args.card_id)})
                return 0
            if args.card_command == "relations":
                _print({"card_id": args.card_id, "relations": card_store.card_relations(args.card_id)})
                return 0
            if args.card_command == "transition":
                _print(
                    card_service.transition_card(
                        args.card_id, args.status, reason=args.reason, actor=args.actor
                    )
                )
                return 0
            if args.card_command == "promote":
                _print(card_service.promote_card(args.card_id, reason=args.reason, actor=args.actor))
                return 0
        if args.command == "graph":
            database, repository, _ = _service(path)
            graph_payload = ExtractionStore(database).graph(
                task_id=args.task_id,
                limit=args.limit,
                include_quarantine=args.include_quarantine,
            )
            _print(
                CardStore(database).extend_graph(
                    graph_payload,
                    task_id=args.task_id,
                    limit=args.limit,
                    include_quarantine=args.include_quarantine,
                )
            )
            return 0
        if args.command == "quality-report":
            _, repository, _ = _service(path)
            quality = QualityService(repository)
            _print(
                quality.report(
                    project_id=args.project_id,
                    task_id=args.task_id,
                    logical_project_id_value=args.logical_project_id,
                )
            )
            return 0
        if args.command == "quality-replay":
            _, repository, _ = _service(path)
            result = QualityService(repository).replay(
                project_id=args.project_id,
                task_id=args.task_id,
                logical_project_id_value=args.logical_project_id,
                limit=args.limit,
                write=args.write,
            )
            _print(result.as_dict())
            return 0
        if args.command in {"verify-bindings", "verify-project-j"}:
            _, repository, _ = _service(path)
            manifests = [_load_json_object(item) for item in (args.manifest or [])]
            p4_manifest = _load_json_object(args.p4_manifest) if args.p4_manifest else None
            codebase_manifest = (
                _load_json_object(args.codebase_memory_manifest)
                if args.codebase_memory_manifest
                else None
            )
            p4_options = {
                "root_path": args.p4_root,
                "port": args.p4_port,
                "user": args.p4_user,
                "client": args.p4_client,
                "executable": args.p4_executable,
                "timeout_seconds": args.p4_timeout,
            }
            result = VerificationService(repository).verify(
                logical_project_id=args.logical_project_id,
                manifests=manifests,
                p4_manifest=p4_manifest,
                codebase_memory_manifest=codebase_manifest,
                p4_paths=args.p4_path,
                p4_options=p4_options,
                task_ids=args.task_id or [],
                write=args.write,
                limit=args.limit,
                include_quarantine=args.include_quarantine,
            )
            _print(result.as_dict())
            return 0 if result.status in {"succeeded", "not_applicable"} else 2
        if args.command == "verification-report":
            _, repository, _ = _service(path)
            _print(
                VerificationService(repository).report(
                    logical_project_id=args.logical_project_id,
                    limit=args.limit,
                    include_manifest=args.include_manifest,
                )
            )
            return 0
        if args.command == "quality-projects":
            _, repository, _ = _service(path)
            _print({"projects": QualityService(repository).store.list_logical_projects(limit=args.limit)})
            return 0
        if args.command == "project-alias":
            _, repository, _ = _service(path)
            try:
                evidence = json.loads(args.evidence_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"--evidence-json must be a JSON object: {exc}") from exc
            if not isinstance(evidence, dict):
                raise ValueError("--evidence-json must decode to an object")
            _print(
                QualityService(repository).register_project_alias(
                    raw_project_id=args.raw_project_id,
                    logical_project_id=args.logical_project_id,
                    alias_value=args.alias_value,
                    alias_type=args.alias_type,
                    normalized_root=args.normalized_root,
                    confidence=args.confidence,
                    evidence=evidence,
                    display_name=args.display_name,
                )
            )
            return 0
        if args.command == "serve":
            import uvicorn

            # create_app initializes the schema before uvicorn starts serving.
            uvicorn.run(create_app(path), host=args.host, port=args.port, log_level="info")
            return 0
    except (
        ValidationError,
        ConflictError,
        ValueError,
        FileNotFoundError,
        FileExistsError,
        OSError,
    ) as exc:
        _print({"error": type(exc).__name__, "message": str(exc)})
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


def validate_schema_file(path: Path) -> dict[str, Any]:
    """Validate a JSON object or one envelope per JSONL line."""

    total = 0
    valid = 0
    invalid = 0
    errors: list[dict[str, Any]] = []
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        records = [
            json.loads(line)
            for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        loaded = json.loads(raw)
        records = loaded if isinstance(loaded, list) else [loaded]
    for index, record in enumerate(records, start=1):
        total += 1
        try:
            EventEnvelope.model_validate(record)
            valid += 1
        except Exception as exc:  # Pydantic's structured details are retained as text.
            invalid += 1
            errors.append({"record": index, "message": str(exc)})
    return {
        "path": str(path.resolve()),
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "errors": errors,
    }


def replay_main(argv: list[str] | None = None) -> int:
    """Dedicated console entry point installed as ``codememory-replay``."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    return main(["replay", *arguments])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
