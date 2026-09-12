"""Shared, testable support for the Codex lifecycle hook adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import default_db_path
from .quality.project_scope import is_codememory_root, normalize_root, root_basename


HOOK_ERROR_PREFIX = "CODEMEMORY_HOOK_ERROR "
_INVALID_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')
_SLUG_RE = re.compile(r"[^A-Za-z0-9._:-]+")
_PROJECT_J_WORKSPACE_MARKER = "/p4workspace/client/mainline"


def clean_unicode_text(value: str) -> str:
    """Replace unpaired UTF-16 surrogate code points without touching valid Unicode."""

    return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value)


def clean_hook_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_unicode_text(value)
    if isinstance(value, list):
        return [clean_hook_value(item) for item in value]
    if isinstance(value, tuple):
        return [clean_hook_value(item) for item in value]
    if isinstance(value, dict):
        return {clean_unicode_text(str(key)): clean_hook_value(item) for key, item in value.items()}
    return value


def _surrogate_count(value: Any) -> int:
    if isinstance(value, str):
        return sum(1 for char in value if 0xD800 <= ord(char) <= 0xDFFF)
    if isinstance(value, list):
        return sum(_surrogate_count(item) for item in value)
    if isinstance(value, dict):
        return sum(_surrogate_count(key) + _surrogate_count(item) for key, item in value.items())
    return 0


def decode_hook_payload(raw: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode the command-hook stdin payload and repair only safe JSON defects."""

    decoded = raw.decode("utf-8-sig", errors="replace")
    diagnostics = {
        "raw_bytes": len(raw),
        "decode_replacements": decoded.count("\ufffd"),
        "json_repaired": False,
        "surrogate_replacements": 0,
    }
    try:
        payload = json.loads(decoded, strict=False) if decoded.strip() else {}
    except json.JSONDecodeError as first_error:
        repaired = _INVALID_JSON_ESCAPE.sub(r"\\\\", decoded)
        if repaired == decoded:
            raise first_error
        payload = json.loads(repaired, strict=False)
        diagnostics["json_repaired"] = True
    if not isinstance(payload, dict):
        raise ValueError("Codex hook payload must be a JSON object")
    diagnostics["surrogate_replacements"] = _surrogate_count(payload)
    cleaned = clean_hook_value(payload)
    return cleaned, diagnostics


def _configured_project_roots() -> list[tuple[str, str]]:
    raw = os.environ.get("CODEMEMORY_PROJECT_ROOTS_JSON", "").strip()
    if not raw:
        return []
    loaded = json.loads(raw)
    if not isinstance(loaded, Mapping):
        raise ValueError("CODEMEMORY_PROJECT_ROOTS_JSON must be a JSON object")
    roots: list[tuple[str, str]] = []
    for project_id, values in loaded.items():
        candidates = values if isinstance(values, list) else [values]
        for candidate in candidates:
            normalized = normalize_root(candidate)
            if normalized:
                roots.append((str(project_id), normalized))
    return sorted(roots, key=lambda item: len(item[1]), reverse=True)


def _is_root_or_child(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def resolve_hook_project(payload: Mapping[str, Any], *, repo_root: Path) -> str:
    """Resolve a stable raw project id for the Codex hook's current workspace."""

    explicit = os.environ.get("CODEMEMORY_PROJECT_ID") or payload.get("project_id")
    if explicit:
        return clean_unicode_text(str(explicit)).strip()
    cwd = clean_unicode_text(str(payload.get("cwd") or os.getcwd()))
    normalized = normalize_root(cwd)
    for project_id, root in _configured_project_roots():
        if _is_root_or_child(normalized, root):
            return project_id
    marker_index = normalized.find(_PROJECT_J_WORKSPACE_MARKER)
    if marker_index >= 0:
        marker_end = marker_index + len(_PROJECT_J_WORKSPACE_MARKER)
        if marker_end == len(normalized) or normalized[marker_end] == "/":
            return "project_j"
    if root_basename(normalized) in {"project_j", "projectj"}:
        return "project_j"
    if is_codememory_root(normalized) or _is_root_or_child(normalized, normalize_root(repo_root)):
        return "codeSementicMemory"
    name = root_basename(normalized) or "workspace"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"codex-project:{name}:{digest}"


def _bounded_identity(prefix: str, *pieces: str) -> str:
    raw = "\n".join(str(piece) for piece in pieces)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    readable = "-".join(
        _SLUG_RE.sub("-", str(piece)).strip("-")[:64] or "value" for piece in pieces
    )
    return f"{prefix}:{readable}:{digest}"[:300]


def hook_cycle_ids(
    *, project_id: str, source_thread_id: str, raw_session_id: str
) -> tuple[str, str]:
    """Namespace task/session identities so one Codex thread may visit multiple projects."""

    task_id = _bounded_identity("codex-hook-task", project_id, source_thread_id)
    session_id = _bounded_identity(
        "codex-hook-session", project_id, source_thread_id, raw_session_id
    )
    return task_id, session_id


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    rendered = json.dumps(clean_hook_value(value), ensure_ascii=False, sort_keys=True, indent=2)
    temporary.write_text(rendered + "\n", encoding="utf-8", errors="replace")
    os.replace(temporary, path)


def write_maintenance_files(
    *,
    prepared: Mapping[str, Any],
    maintenance: Mapping[str, Any],
    model: str,
) -> tuple[Path, Path]:
    """Write a bounded evidence packet and editable learn payload for the current Agent."""

    root = Path(
        os.environ.get(
            "CODEMEMORY_MAINTENANCE_DIR",
            str(default_db_path().with_name("maintenance")),
        )
    )
    maintenance_id = str(maintenance["maintenance_id"])
    request_path = root / f"{maintenance_id}.request.json"
    learn_path = root / f"{maintenance_id}.learn.json"
    packet = dict(prepared)
    packet["maintenance"] = dict(maintenance)
    _atomic_write_json(request_path, packet)
    learn_payload = dict(prepared.get("learn_schema") or {})
    learn_payload.update(
        {
            "turn_id": f"maintenance:{maintenance_id}",
            "input_hash": str(prepared["input_hash"]),
            "model": model or "current-codex",
            "notes": [],
        }
    )
    preserve_existing = False
    if learn_path.exists():
        try:
            existing = json.loads(learn_path.read_text(encoding="utf-8"))
            preserve_existing = (
                isinstance(existing, dict)
                and existing.get("input_hash") == learn_payload["input_hash"]
            )
        except (OSError, json.JSONDecodeError):
            preserve_existing = False
    if not preserve_existing:
        _atomic_write_json(learn_path, learn_payload)
    return request_path, learn_path


def append_hook_error(
    exc: BaseException,
    *,
    payload: Mapping[str, Any] | None = None,
    stage: str,
    diagnostics: Mapping[str, Any] | None = None,
) -> None:
    """Append one machine-readable, content-minimal fail-open error record."""

    try:
        source = payload or {}
        message_lines: list[str] = []
        for line in clean_unicode_text(str(exc)).splitlines():
            if "input_value=" in line or "input_type=" in line:
                continue
            message_lines.append(line.strip())
            if len(message_lines) >= 3:
                break
        frames = [
            {
                "file": Path(frame.filename).name,
                "line": frame.lineno,
                "function": frame.name,
            }
            for frame in traceback.extract_tb(exc.__traceback__)[-12:]
        ]
        path = Path(
            os.environ.get(
                "CODEMEMORY_HOOK_LOG",
                str(default_db_path().with_name("codememory-hook.log")),
            )
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = clean_hook_value(
            {
                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "stage": stage,
                "hook_event_name": source.get("hook_event_name"),
                "project_id": source.get("project_id"),
                "session_id": source.get("session_id"),
                "thread_id": source.get("thread_id"),
                "cwd": source.get("cwd"),
                "error_type": type(exc).__name__,
                "message": " | ".join(message_lines)[:2000],
                "diagnostics": dict(diagnostics or {}),
                "frames": frames,
            }
        )
        line = HOOK_ERROR_PREFIX + json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(line + "\n")
    except Exception:
        pass
