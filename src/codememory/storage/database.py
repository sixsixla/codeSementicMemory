"""SQLite connection and migration management."""

from __future__ import annotations

import sqlite3
import os
import tempfile
import sys
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    """Small, concurrency-safe SQLite database facade.

    A connection is short lived by design. WAL mode lets the future ingest
    process and read-only UI coexist without introducing a server database.
    """

    def __init__(self, path: str | Path, migrations_dir: str | Path | None = None):
        self.path = Path(path)
        self.migrations_dir = (
            Path(migrations_dir) if migrations_dir else self._default_migrations_dir()
        )
        # A long extraction replay may call ``ensure_initialized`` once per
        # task.  Migrations are immutable for the lifetime of a Database
        # instance, so cache the successful check while retaining a lock for
        # concurrent API/worker callers.  This removes thousands of redundant
        # filesystem reads without changing migration semantics.
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @staticmethod
    def _default_migrations_dir() -> Path:
        # Source checkout is the primary distribution. The cwd fallback makes
        # an installed editable package work when invoked from the repository.
        candidates = (
            Path(__file__).resolve().parents[3] / "migrations",
            Path(__file__).resolve().parents[2] / "migrations",
            Path.cwd() / "migrations",
            Path(sys.prefix) / "migrations",
            Path(sys.base_prefix) / "migrations",
        )
        return next((path for path in candidates if path.exists()), candidates[0])

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def initialize(self) -> None:
        """Apply all ordered SQL migrations exactly once."""

        with self.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            }
            for migration in sorted(self.migrations_dir.glob("*.sql")):
                version = migration.name
                if version in applied:
                    continue
                sql = migration.read_text(encoding="utf-8")
                safe_version = version.replace("'", "''")
                try:
                    from datetime import datetime, timezone

                    applied_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    conn.executescript(
                        "BEGIN;\n"
                        + sql
                        + "\nINSERT INTO schema_migrations(version, applied_at) VALUES ('"
                        + safe_version
                        + "', '"
                        + applied_at.replace("'", "''")
                        + "');\nCOMMIT;"
                    )
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    raise

    def ensure_initialized(self) -> None:
        # initialize() is idempotent and also repairs a database left between
        # migrations after a process interruption.
        if self._initialized:
            return
        with self._initialize_lock:
            if not self._initialized:
                self.initialize()
                self._initialized = True

    def migration_version(self) -> str | None:
        self.ensure_initialized()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            return str(row[0]) if row else None

    def integrity_check(self, *, deep: bool = False) -> str:
        """Run a bounded health check (or the optional full check).

        ``PRAGMA integrity_check`` scans every index/table and becomes
        prohibitively slow for a large local history archive.  The default
        ``quick_check`` is suitable for request-path health probes; operators
        can request the exhaustive scan with ``deep=True`` during maintenance.
        """

        self.ensure_initialized()
        with self.connection() as conn:
            pragma = "integrity_check" if deep else "quick_check"
            return str(conn.execute(f"PRAGMA {pragma}").fetchone()[0])

    def deep_integrity_check(self) -> str:
        """Run SQLite's exhaustive integrity scan explicitly."""

        return self.integrity_check(deep=True)

    def journal_mode(self) -> str:
        with self.connection() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def backup_to(self, destination: str | Path) -> Path:
        """Create a consistent SQLite backup without stopping the service."""

        self.ensure_initialized()
        target = Path(destination)
        if target.resolve() == self.path.resolve():
            raise ValueError("backup destination must differ from the source database")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source, closing(sqlite3.connect(str(target))) as backup:
            source.backup(backup)
            backup.commit()
        return target

    @staticmethod
    def restore_from(source: str | Path, destination: str | Path, *, force: bool = False) -> Path:
        """Restore a backup atomically; replacing an existing DB needs force."""

        source_path = Path(source)
        target = Path(destination)
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        if target.exists() and not force:
            raise FileExistsError(f"destination exists; pass force=True to replace: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix="codememory-restore-", suffix=".sqlite3", dir=target.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with (
                closing(sqlite3.connect(str(source_path))) as source_conn,
                closing(sqlite3.connect(str(temporary))) as target_conn,
            ):
                source_conn.backup(target_conn)
                target_conn.commit()
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target
