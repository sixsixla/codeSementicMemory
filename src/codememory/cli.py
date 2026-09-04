"""Command line interface for local development and replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .api.app import create_app
from .adapters import CliProducer
from .config import default_db_path, event_schema_path
from .domain.events import EventEnvelope, EventType
from .ingest.service import IngestService, replay_jsonl
from .storage.database import Database
from .storage.repository import ConflictError, MemoryRepository


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

    outbox = sub.add_parser("outbox", help="inspect or claim outbox jobs")
    outbox.add_argument("--db", help="database path")
    outbox.add_argument("--status")
    outbox.add_argument("--limit", type=int, default=100)
    outbox.add_argument("--claim", action="store_true")
    outbox.add_argument("--lease-seconds", type=int, default=60)

    serve = sub.add_parser("serve", help="run the localhost HTTP API")
    serve.add_argument("--db", help="database path")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


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
            _print(repository.health())
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
        if args.command == "outbox":
            _, repository, _ = _service(path)
            jobs = (
                repository.claim_outbox(limit=args.limit, lease_seconds=args.lease_seconds)
                if args.claim
                else repository.list_outbox(status=args.status, limit=args.limit)
            )
            _print({"counts": repository.outbox_count(), "jobs": [job.as_dict() for job in jobs]})
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
