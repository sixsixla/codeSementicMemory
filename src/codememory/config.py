"""Runtime configuration helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_db_path() -> Path:
    configured = os.environ.get("CODEMEMORY_DB")
    if configured:
        return Path(configured)
    data_dir = os.environ.get("CODEMEMORY_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "codememory.sqlite3"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "CodeSementicMemory" / "codememory.sqlite3"
    return Path.home() / ".codememory" / "codememory.sqlite3"


def event_schema_path() -> Path:
    """Locate the schema in a source checkout or a data-file installation."""

    candidates = (
        Path(__file__).resolve().parents[2] / "schemas" / "codememory.event.v1.json",
        Path.cwd() / "schemas" / "codememory.event.v1.json",
        Path(sys.prefix) / "schemas" / "codememory.event.v1.json",
        Path(sys.base_prefix) / "schemas" / "codememory.event.v1.json",
    )
    return next((path for path in candidates if path.exists()), candidates[0])
