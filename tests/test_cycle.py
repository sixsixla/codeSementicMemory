from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from codememory.cycle import (
    AgentMemoryCycleService,
    CycleCheckpointRequest,
    CycleCloseRequest,
    CycleOpenRequest,
)
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def _base() -> dict[str, object]:
    return {
        "project_id": "project_j",
        "source_thread_id": "thread-cycle-1",
        "session_id": "session-cycle-1",
        "context": {"root_path": "D:/Project_J", "cwd": "D:/Project_J"},
    }


def test_cycle_is_incremental_and_idempotent(tmp_path: Path) -> None:
    repository = MemoryRepository(Database(tmp_path / "cycle.sqlite3"))
    service = AgentMemoryCycleService(repository)

    opened = service.open(CycleOpenRequest(**_base(), prompt="NPC share panel", turn_id="turn-1"))
    assert opened["task_id"] == "codex-thread:thread-cycle-1"
    assert opened["cycle"]["prompt_count"] == 1

    repeated = service.open(CycleOpenRequest(**_base(), prompt="NPC share panel", turn_id="turn-1"))
    assert repeated["cycle"]["prompt_count"] == 1
    assert len(repository.timeline(opened["task_id"], session_id=opened["session_id"])) == 2

    checkpoint = service.checkpoint(
        CycleCheckpointRequest(
            **_base(),
            turn_id="turn-1-stop",
            summary="implemented and compiled",
            modified_files=["Assets/Scripts/Npc.cs"],
            outcome="success",
            extract=False,
            consolidate=False,
        )
    )
    assert checkpoint["cycle"]["status"] == "checkpointed"
    assert checkpoint["capture"]["event_count"] == 2

    closed = service.close(
        CycleCloseRequest(
            **_base(),
            summary="done",
            outcome="success",
            extract=False,
            consolidate=False,
        )
    )
    assert closed["cycle"]["status"] == "closed"
    assert repository.get_agent_memory_cycle(
        source_system="codex",
        source_thread_id="thread-cycle-1",
        project_id="project_j",
        session_id="session-cycle-1",
    )["last_event_seq"] >= 4


def test_codex_hook_user_prompt_is_fail_open_and_returns_context(tmp_path: Path) -> None:
    db_path = tmp_path / "hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "hook-session-1",
        "turn_id": "hook-turn-1",
        "prompt": "NPC share panel",
        "cwd": "D:/Project_J",
        "model": "test-model",
    }
    env = dict(os.environ)
    env["CODEMEMORY_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "CodeMemory" in output["hookSpecificOutput"]["additionalContext"]


def test_codex_hook_captures_visible_tool_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "tool-hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "tool-session-1",
        "turn_id": "tool-turn-1",
        "tool_use_id": "tool-call-1",
        "tool_name": "apply_patch",
        "tool_input": {"file_path": "Assets/Scripts/Npc.cs"},
        "tool_response": "patched",
        "cwd": "D:/Project_J",
    }
    env = dict(os.environ)
    env["CODEMEMORY_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    repository = MemoryRepository(Database(db_path))
    events = repository.export_events(project_id="project_j")
    assert any(event["event_type"] == "file_edit" for event in events)
