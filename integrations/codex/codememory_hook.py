"""Codex lifecycle hook for the local CodeSementicMemory loop.

The hook is deliberately fail-open: a memory failure is logged and never
blocks the coding agent.  It only consumes visible prompt/assistant fields
provided by Codex; hidden reasoning and private transcript state are not read.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from codememory.config import default_db_path  # noqa: E402
from codememory.cycle import (  # noqa: E402
    AgentMemoryCycleService,
    CycleCheckpointRequest,
    CycleCloseRequest,
    CycleOpenRequest,
)
from codememory.storage.database import Database  # noqa: E402
from codememory.storage.repository import MemoryRepository  # noqa: E402


def _project_id(payload: dict[str, Any]) -> str:
    explicit = os.environ.get("CODEMEMORY_PROJECT_ID") or payload.get("project_id")
    if explicit:
        return str(explicit)
    cwd = str(payload.get("cwd") or os.getcwd())
    lowered = cwd.lower().replace("\\", "/")
    if "project_j" in lowered or "/projectj" in lowered:
        return "project_j"
    if "codesementicmemory" in lowered:
        return "codeSementicMemory"
    name = Path(cwd).name or "workspace"
    digest = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:12]
    return f"codex-project:{name}:{digest}"


def _source_thread_id(payload: dict[str, Any]) -> str:
    return str(payload.get("thread_id") or payload.get("session_id") or "codex-sessionless")


def _base(payload: dict[str, Any]) -> dict[str, Any]:
    cwd = str(payload.get("cwd") or os.getcwd())
    return {
        "project_id": _project_id(payload),
        "source_thread_id": _source_thread_id(payload),
        "session_id": str(payload.get("session_id") or _source_thread_id(payload)),
        "agent_id": "codex",
        "adapter": "codex-hook",
        "adapter_version": "0.1",
        "context": {
            "cwd": cwd,
            "root_path": cwd,
            "model": payload.get("model"),
            "hook_event_name": payload.get("hook_event_name"),
        },
    }


def _route_context(result: dict[str, Any]) -> str:
    query = result.get("query") or {}
    cards = query.get("cards") or []
    if not cards:
        return "[CodeMemory] No route cards matched this prompt yet. Treat new code discovery as evidence for the next checkpoint."
    lines = ["[CodeMemory route hints] Historical coding evidence (hypotheses; verify against current source):"]
    for index, card in enumerate(cards[:8], start=1):
        route = card.get("route") or {}
        entries = route.get("entrypoints") or card.get("entrypoints") or []
        entry_text = ", ".join(
            str(item.get("qualified_symbol") or item.get("symbol") or item.get("path"))
            for item in entries[:5]
            if isinstance(item, dict)
        )
        if not entry_text:
            entry_text = "(no verified entrypoint; inspect card evidence)"
        statement = str(card.get("statement") or card.get("summary") or "")
        lines.append(
            f"{index}. {statement} | card={card.get('card_id')} | "
            f"trust={route.get('trust_level', 'candidate')} score={route.get('score', 0)} | "
            f"entrypoints={entry_text}"
        )
    lines.append("Use only as a fast starting point; current files and tests remain authoritative.")
    return "\n".join(lines)


def _tool_paths(value: Any, *, limit: int = 30) -> list[str]:
    paths: list[str] = []
    path_keys = {"path", "file", "file_path", "filepath", "filename", "target"}

    def visit(item: Any, key: str = "") -> None:
        if len(paths) >= limit:
            return
        if isinstance(item, dict):
            for child_key, child in item.items():
                visit(child, str(child_key).casefold())
        elif isinstance(item, list):
            for child in item:
                visit(child, key)
        elif key in path_keys and isinstance(item, str) and item.strip():
            paths.append(item.strip()[:1000])

    visit(value)
    return list(dict.fromkeys(paths))


def _log_error(exc: BaseException) -> None:
    try:
        path = Path(os.environ.get("CODEMEMORY_HOOK_LOG", str(default_db_path().with_name("codememory-hook.log"))))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
            handle.write("\n")
    except Exception:
        pass


def _service() -> AgentMemoryCycleService:
    database = Database(os.environ.get("CODEMEMORY_DB") or default_db_path())
    return AgentMemoryCycleService(MemoryRepository(database))


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("hook_event_name") or "")
    base = _base(payload)
    cycle = _service()
    if event == "SessionStart":
        cycle.open(CycleOpenRequest(**base))
        return {}
    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return {}
        result = cycle.open(
            CycleOpenRequest(
                **base,
                prompt=prompt,
                turn_id=str(payload.get("turn_id") or "prompt"),
                retrieval_mode="route",
                limit=8,
            )
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _route_context(result),
            }
        }
    if event == "Stop":
        if payload.get("stop_hook_active"):
            return {}
        message = str(payload.get("last_assistant_message") or "").strip()
        if not message:
            return {}
        cycle.checkpoint(
            CycleCheckpointRequest(
                **base,
                turn_id=str(payload.get("turn_id") or "stop"),
                summary=message,
                outcome="unknown",
                provider=os.environ.get("CODEMEMORY_HOOK_PROVIDER", "mock"),
                extract=True,
                consolidate=True,
            )
        )
        return {}
    if event == "PostToolUse":
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "tool")
        tool_input = payload.get("tool_input")
        tool_output = str(payload.get("tool_output") or "")
        lowered = tool_name.casefold()
        paths = _tool_paths(tool_input)
        modified = paths if any(word in lowered for word in ("edit", "write", "patch", "create", "replace", "move")) else []
        explored = paths if not modified and any(word in lowered for word in ("read", "search", "grep", "rg", "glob", "list", "find")) else []
        summary = f"{tool_name}: {tool_output[:3000]}".strip()
        if not modified and not explored and not summary:
            return {}
        cycle.checkpoint(
            CycleCheckpointRequest(
                **base,
                turn_id=str(payload.get("tool_call_id") or payload.get("turn_id") or f"tool:{tool_name}"),
                explored_files=explored,
                modified_files=modified,
                summary=summary or tool_name,
                outcome="unknown",
                extract=False,
                consolidate=False,
            )
        )
        return {}
    if event == "SessionEnd":
        message = str(payload.get("last_assistant_message") or "").strip() or None
        cycle.close(
            CycleCloseRequest(
                **base,
                summary=message,
                outcome="unknown",
                provider=os.environ.get("CODEMEMORY_HOOK_PROVIDER", "mock"),
                extract=False,
                consolidate=False,
            )
        )
        return {}
    return {}


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        result = handle(payload if isinstance(payload, dict) else {})
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:  # fail-open by design
        _log_error(exc)
        print("{}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
