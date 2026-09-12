"""Codex lifecycle hook for the local CodeSementicMemory loop.

The hook is deliberately fail-open: a memory failure is logged and never
blocks the coding agent.  It only consumes visible prompt/assistant fields
provided by Codex; hidden reasoning and private transcript state are not read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from codememory.config import default_db_path  # noqa: E402
from codememory.cycle import (  # noqa: E402
    AgentMemoryCycleService,
    CycleBaseRequest,
    CycleCheckpointRequest,
    CycleCloseRequest,
    CycleOpenRequest,
)
from codememory.hook_support import (  # noqa: E402
    append_hook_error,
    clean_hook_value,
    clean_unicode_text,
    decode_hook_payload,
    hook_cycle_ids,
    resolve_hook_project,
    write_maintenance_files,
)
from codememory.storage.database import Database  # noqa: E402
from codememory.storage.repository import MemoryRepository  # noqa: E402


_PATH_TOKEN_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/][^\s\"'<>|]+|"
    r"(?:[\w.@()+ -]+[\\/]){1,}[\w.@()+ -]+\."
    r"(?:cs|py|json|jsonl|md|sql|toml|yaml|yml|xml|prefab|asset|meta|cpp|h|hpp|c|js|ts|tsx|jsx))"
)
_VALIDATION_COMMANDS = (
    "pytest",
    "ruff ",
    "compileall",
    "dotnet build",
    "dotnet test",
    "msbuild",
    "unity",
    "git diff",
    "git status",
    "p4 diff",
    "p4 opened",
    "p4 fstat",
)
_READ_COMMANDS = (
    "get-content",
    "select-string",
    "codex-rg",
    "rg ",
    "git show",
    "git diff",
    "type ",
    "cat ",
)
_WRITE_COMMANDS = (
    "apply_patch",
    "*** update file:",
    "*** add file:",
    "*** delete file:",
    "set-content",
    "add-content",
    "move-item",
    "copy-item",
    "ruff format",
)


def _project_id(payload: dict[str, Any]) -> str:
    return resolve_hook_project(payload, repo_root=REPO_ROOT)


def _source_thread_id(payload: dict[str, Any]) -> str:
    return clean_unicode_text(
        str(payload.get("thread_id") or payload.get("session_id") or "codex-sessionless")
    )


def _base(payload: dict[str, Any]) -> dict[str, Any]:
    cwd = clean_unicode_text(str(payload.get("cwd") or os.getcwd()))
    project_id = _project_id(payload)
    source_thread_id = _source_thread_id(payload)
    raw_session_id = clean_unicode_text(str(payload.get("session_id") or source_thread_id))
    task_id, session_id = hook_cycle_ids(
        project_id=project_id,
        source_thread_id=source_thread_id,
        raw_session_id=raw_session_id,
    )
    return {
        "project_id": project_id,
        "source_thread_id": source_thread_id,
        "task_id": task_id,
        "session_id": session_id,
        "agent_id": "codex",
        "adapter": "codex-hook",
        "adapter_version": "0.2",
        "context": {
            "cwd": cwd,
            "root_path": cwd,
            "model": payload.get("model"),
            "hook_event_name": payload.get("hook_event_name"),
            "codex_session_id": raw_session_id,
            "hook_input": dict(payload.get("_codememory_input_diagnostics") or {}),
        },
    }


def _route_context(result: dict[str, Any]) -> str:
    query = result.get("query") or {}
    cards = query.get("cards") or []
    if not cards:
        return "[CodeMemory] No route cards matched this prompt yet. Treat new code discovery as evidence for the next checkpoint."
    lines = [
        "[CodeMemory route hints] Historical coding evidence (hypotheses; verify against current source):"
    ]
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
    path_keys = {
        "path",
        "paths",
        "file",
        "files",
        "file_path",
        "filepath",
        "filename",
        "target",
    }

    def add_text(text: str) -> None:
        for match in _PATH_TOKEN_RE.findall(text):
            cleaned = match.strip().rstrip(",;:)]}")[:1000]
            if cleaned:
                paths.append(cleaned)
                if len(paths) >= limit:
                    return

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
        elif key in {"command", "cmd", "patch"} and isinstance(item, str):
            for line in item.splitlines():
                if "*** " in line and " File:" in line:
                    paths.append(line.split(" File:", 1)[1].strip()[:1000])
            add_text(item)

    visit(value)
    return list(dict.fromkeys(paths))


def _tool_command(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("command", "cmd", "patch"):
        command = value.get(key)
        if isinstance(command, str):
            return command[:5000]
    return ""


def _validation(command: str, response: Any) -> list[dict[str, Any]]:
    lowered = command.casefold()
    if not command or not any(token in lowered for token in _VALIDATION_COMMANDS):
        return []
    exit_code = response.get("exit_code") if isinstance(response, dict) else None
    if exit_code is None and isinstance(response, dict):
        exit_code = response.get("exitCode")
    output = clean_unicode_text(str(response if response is not None else ""))[:3000]
    return [
        {
            "command": command[:2000],
            "exit_code": exit_code,
            "status": "passed" if exit_code == 0 else "unknown",
            "output": output,
        }
    ]


def _memory_operation(payload: dict[str, Any]) -> bool:
    raw = json.dumps(clean_hook_value(payload.get("tool_input")), ensure_ascii=True).casefold()
    return (
        "-m codememory" in raw
        or "codememory_hook.py" in raw
        or "agent cycle" in raw
        or "codememory-cycle" in raw
        or "cycle-learn" in raw
    )


def _powershell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _content_turn_id(identity: Any, content: Any) -> str:
    base = clean_unicode_text(str(identity or "turn"))[:450]
    canonical = json.dumps(
        clean_hook_value(content),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{base}:{digest}"[:500]


def _current_turn_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_sequences = [
        int(item.get("seq", -1)) for item in events if str(item.get("event_type")) == "user_message"
    ]
    if not prompt_sequences:
        return events
    latest_prompt = max(prompt_sequences)
    return [item for item in events if int(item.get("seq", -1)) >= latest_prompt]


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
        if not prompt or prompt.startswith("[CODEMEMORY_MAINTENANCE]"):
            return {}
        result = cycle.open(
            CycleOpenRequest(
                **base,
                prompt=prompt,
                turn_id=_content_turn_id(payload.get("turn_id") or "prompt", prompt),
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
            cycle.fail_pending_maintenance(
                CycleBaseRequest(**base),
                error="Codex maintenance continuation ended without a successful cycle learn.",
            )
            return {}
        message = str(payload.get("last_assistant_message") or "").strip()
        if not message:
            return {}
        provider = os.environ.get("CODEMEMORY_HOOK_PROVIDER", "agent")
        stop_turn_id = _content_turn_id(
            payload.get("turn_id") or "stop",
            {"message": message, "outcome": "unknown"},
        )
        cycle.checkpoint(
            CycleCheckpointRequest(
                **base,
                turn_id=stop_turn_id,
                summary=message,
                outcome="unknown",
                provider=provider if provider != "agent" else "mock",
                extract=provider != "agent",
                consolidate=provider != "agent",
            )
        )
        if provider == "agent":
            prepared = cycle.prepare(CycleOpenRequest(**base))
            turn_events = _current_turn_events(list(prepared.get("events", [])))
            coding_events = {
                "file_read",
                "file_edit",
                "tool_result",
                "validation_run",
                "command_run",
                "vcs_change",
            }
            has_code_evidence = any(
                str(item.get("event_type")) in coding_events for item in turn_events
            ) or any(
                token in message.casefold()
                for token in (
                    ".cs",
                    ".py",
                    "compile",
                    "test",
                    "implemented",
                    "修改",
                    "修复",
                    "代码",
                )
            )
            if has_code_evidence:
                maximum = max(
                    1,
                    int(os.environ.get("CODEMEMORY_MAINTENANCE_MAX_ATTEMPTS", "2")),
                )
                maintenance = cycle.request_maintenance(
                    CycleBaseRequest(**base),
                    input_hash=str(prepared["input_hash"]),
                    turn_id=stop_turn_id,
                    max_attempts=maximum,
                    provider="agent",
                    model=str(payload.get("model") or "current-codex"),
                    metadata={"trigger": "Stop", "has_code_evidence": True},
                )
                if maintenance["status"] != "pending":
                    return {}
                try:
                    request_path, learn_path = write_maintenance_files(
                        prepared=prepared,
                        maintenance=maintenance,
                        model=str(payload.get("model") or "current-codex"),
                    )
                except Exception as exc:
                    cycle.repository.fail_agent_memory_maintenance(
                        cycle_id=str(maintenance["cycle_id"]),
                        input_hash=str(maintenance["input_hash"]),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    raise
                db = os.environ.get("CODEMEMORY_DB")
                db_arg = f" --db {_powershell_quote(db)}" if db else ""
                command = (
                    f"Set-Location {_powershell_quote(str(REPO_ROOT))}; "
                    f"py -3 -X utf8 -m codememory agent cycle learn "
                    f"{_powershell_quote(str(learn_path))}{db_arg}"
                )
                reason = (
                    "[CODEMEMORY_MAINTENANCE] Execute this bounded maintenance continuation now; "
                    "it is not a new coding task. Read the evidence packet at "
                    f"{request_path}. Edit only the `notes` array in {learn_path}: use at most 8 "
                    "useful coding notes with exact evidence_event_ids and binding.evidence; an empty "
                    "array is valid, and verification must not be invented. Then run exactly: "
                    f"{command}. Do not merely describe these steps and do not edit project source. "
                    f"maintenance_id={maintenance['maintenance_id']} "
                    f"attempt={maintenance['attempts']}/{maintenance['max_attempts']}."
                )
                return {"decision": "block", "reason": reason}
        return {}
    if event == "PostToolUse":
        if _memory_operation(payload):
            return {}
        tool_name = str(payload.get("tool_name") or payload.get("tool") or "tool")
        tool_input = payload.get("tool_input")
        tool_response = payload.get("tool_response")
        tool_output = clean_unicode_text(
            str(tool_response if tool_response is not None else payload.get("tool_output") or "")
        )
        lowered = tool_name.casefold()
        command = _tool_command(tool_input)
        command_lowered = command.casefold()
        paths = list(
            dict.fromkeys([*_tool_paths(tool_input), *_tool_paths({"command": tool_output[:5000]})])
        )[:30]
        modified = (
            paths
            if (
                any(
                    word in lowered
                    for word in ("edit", "write", "patch", "create", "replace", "move")
                )
                or any(token in command_lowered for token in _WRITE_COMMANDS)
            )
            else []
        )
        explored = (
            paths
            if not modified
            and (
                any(
                    word in lowered
                    for word in ("read", "search", "grep", "rg", "glob", "list", "find")
                )
                or any(token in command_lowered for token in _READ_COMMANDS)
            )
            else []
        )
        validations = _validation(
            command, tool_response if tool_response is not None else tool_output
        )
        if not modified and not explored and not validations:
            return {}
        cycle.checkpoint(
            CycleCheckpointRequest(
                **base,
                turn_id=_content_turn_id(
                    payload.get("tool_use_id")
                    or payload.get("tool_call_id")
                    or payload.get("turn_id")
                    or f"tool:{tool_name}",
                    {"tool_input": tool_input, "tool_response": tool_response},
                ),
                explored_files=explored,
                modified_files=modified,
                validations=validations,
                outcome="partial",
                extract=False,
                consolidate=False,
            )
        )
        return {}
    if event == "SessionEnd":
        close_base = dict(base)
        close_context = dict(base["context"])
        close_context.pop("hook_input", None)
        close_base["context"] = close_context
        cycle.close(
            CycleCloseRequest(
                **close_base,
                summary=None,
                outcome="unknown",
                provider=os.environ.get("CODEMEMORY_HOOK_PROVIDER", "mock"),
                extract=False,
                consolidate=False,
            )
        )
        return {}
    return {}


def main() -> int:
    payload: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    stage = "decode"
    try:
        raw = sys.stdin.buffer.read()
        payload, diagnostics = decode_hook_payload(raw)
        payload["_codememory_input_diagnostics"] = diagnostics
        stage = "handle"
        result = handle(payload)
        print(
            json.dumps(
                clean_hook_value(result),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as exc:  # fail-open by design
        append_hook_error(
            exc,
            payload=payload,
            stage=stage,
            diagnostics=diagnostics,
        )
        print("{}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
