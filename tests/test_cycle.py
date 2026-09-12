from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from codememory.cycle import (
    AgentMemoryNote,
    AgentMemoryCycleService,
    CycleBaseRequest,
    CycleCheckpointRequest,
    CycleCloseRequest,
    CycleLearnRequest,
    CycleOpenRequest,
)
from codememory.extraction.models import Binding, BindingRole
from codememory.hook_health import HookHealthService
from codememory.hook_support import (
    HOOK_ERROR_PREFIX,
    append_hook_error,
    decode_hook_payload,
    hook_cycle_ids,
    resolve_hook_project,
)
from codememory.maintenance import ProjectionMaintenance
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
    assert (
        repository.get_agent_memory_cycle(
            source_system="codex",
            source_thread_id="thread-cycle-1",
            project_id="project_j",
            session_id="session-cycle-1",
        )["last_event_seq"]
        >= 4
    )


def test_current_agent_notes_are_validated_by_normal_extraction_path(tmp_path: Path) -> None:
    repository = MemoryRepository(Database(tmp_path / "learn.sqlite3"))
    service = AgentMemoryCycleService(repository)
    base = _base()
    service.open(CycleOpenRequest(**base, prompt="NPC share panel", turn_id="learn-prompt"))
    service.checkpoint(
        CycleCheckpointRequest(
            **base,
            turn_id="learn-stop",
            summary="implemented Assets/Scripts/Npc.cs and compiled",
            modified_files=["Assets/Scripts/Npc.cs"],
            outcome="success",
            extract=False,
            consolidate=False,
        )
    )
    prepared = service.prepare(CycleBaseRequest(**base))
    edit_event = next(event for event in prepared["events"] if event["event_type"] == "file_edit")
    note = AgentMemoryNote(
        statement="NPC interaction routes to the share panel through the modified integration file.",
        aliases=["NPC share", "interaction share"],
        bindings=[
            Binding(
                role=BindingRole.MODIFIED_FILE,
                path="Assets/Scripts/Npc.cs",
                evidence=[edit_event["event_id"]],
            )
        ],
        evidence_event_ids=[edit_event["event_id"]],
    )
    result = service.learn(
        CycleLearnRequest(
            **base,
            turn_id="learn-submit",
            input_hash=prepared["input_hash"],
            model="gpt-test",
            notes=[note],
        )
    )
    assert result["extraction"]["status"] == "extracted"
    assert result["extraction"]["candidate_count"] == 1


def test_codex_hook_user_prompt_is_fail_open_and_returns_context(tmp_path: Path) -> None:
    db_path = tmp_path / "hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "hook-session-1",
        "turn_id": "hook-turn-1",
        "prompt": "NPC share panel",
        "cwd": "D:/P4Workspace/client/mainline",
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
    repository = MemoryRepository(Database(db_path))
    events = repository.export_events(project_id="project_j")
    assert any(event["event_type"] == "user_message" for event in events)
    assert events[0]["task_id"].startswith("codex-hook-task:")
    assert events[0]["session_id"].startswith("codex-hook-session:")


def test_hook_payload_repair_preserves_chinese_and_replaces_surrogates() -> None:
    raw = (
        rb'{"hook_event_name":"UserPromptSubmit","cwd":"D:\P4Workspace\client\mainline",'
        rb'"prompt":"\u4e2d\u6587\udc80"}'
    )
    payload, diagnostics = decode_hook_payload(raw)
    assert payload["cwd"] == r"D:\P4Workspace\client\mainline"
    assert payload["prompt"] == "中文\ufffd"
    assert diagnostics["json_repaired"] is True
    assert diagnostics["surrogate_replacements"] == 1


def test_hook_project_and_cycle_ids_are_project_scoped() -> None:
    repo_root = Path(__file__).parents[1]
    assert (
        resolve_hook_project({"cwd": r"D:\P4Workspace\client\mainline"}, repo_root=repo_root)
        == "project_j"
    )
    project_task, project_session = hook_cycle_ids(
        project_id="project_j",
        source_thread_id="thread-1",
        raw_session_id="session-1",
    )
    other_task, other_session = hook_cycle_ids(
        project_id="other",
        source_thread_id="thread-1",
        raw_session_id="session-1",
    )
    assert project_task != other_task
    assert project_session != other_session


def test_same_codex_session_can_write_two_projects_without_identity_conflict(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cross-project-hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    env = dict(os.environ)
    env["CODEMEMORY_DB"] = str(db_path)
    env.pop("CODEMEMORY_PROJECT_ID", None)
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "shared-codex-session",
        "thread_id": "shared-codex-thread",
        "turn_id": "turn-1",
        "prompt": "inspect the current project",
    }
    first = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({**payload, "cwd": "D:/P4Workspace/client/mainline"}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({**payload, "cwd": str(Path(__file__).parents[1])}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert "hookSpecificOutput" in json.loads(first.stdout)
    assert "hookSpecificOutput" in json.loads(second.stdout)
    repository = MemoryRepository(Database(db_path))
    assert repository.export_events(project_id="project_j")
    assert repository.export_events(project_id="codeSementicMemory")


def test_same_hook_turn_with_changed_visible_content_uses_new_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "changed-content-hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    env = dict(os.environ)
    env["CODEMEMORY_DB"] = str(db_path)
    base = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "changed-content-session",
        "thread_id": "changed-content-thread",
        "turn_id": "same-turn-id",
        "cwd": "D:/P4Workspace/client/mainline",
    }
    for prompt in ("inspect NPC routing", "inspect share-panel routing"):
        result = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps({**base, "prompt": prompt}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert "hookSpecificOutput" in json.loads(result.stdout)
    repository = MemoryRepository(Database(db_path))
    events = repository.export_events(project_id="project_j")
    assert [event["event_type"] for event in events].count("user_message") == 2


def test_codex_hook_captures_visible_tool_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "tool-hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "tool-session-1",
        "turn_id": "tool-turn-1",
        "tool_use_id": "tool-call-1",
        "tool_name": "Bash",
        "tool_input": {"command": "*** Update File: Assets/Scripts/Npc.cs\n@@\n*** End Patch"},
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
    assert [event["event_type"] for event in events].count("file_edit") == 1
    assert not any(event["event_type"] == "assistant_message" for event in events)


def test_irrelevant_tool_hook_does_not_create_memory_events(tmp_path: Path) -> None:
    db_path = tmp_path / "ignored-tool-hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "ignored-tool-session",
        "tool_use_id": "ignored-tool-call",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "tool_response": {"exitCode": 0, "output": "hello"},
        "cwd": "D:/P4Workspace/client/mainline",
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
    assert json.loads(result.stdout) == {}
    repository = MemoryRepository(Database(db_path))
    assert repository.export_events(project_id="project_j") == []


def test_malformed_hook_json_is_fail_open_and_structured_in_log(tmp_path: Path) -> None:
    db_path = tmp_path / "malformed-hook.sqlite3"
    hook_log = tmp_path / "malformed-hook.log"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    env = dict(os.environ)
    env["CODEMEMORY_DB"] = str(db_path)
    env["CODEMEMORY_HOOK_LOG"] = str(hook_log)
    result = subprocess.run(
        [sys.executable, str(hook)],
        input=b'{"hook_event_name":"SessionStart" "cwd":"D:/Project"}',
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout.decode("utf-8")) == {}
    line = hook_log.read_text(encoding="utf-8").strip()
    assert line.startswith(HOOK_ERROR_PREFIX)
    error = json.loads(line[len(HOOK_ERROR_PREFIX) :])
    assert error["stage"] == "decode"
    assert error["error_type"] == "JSONDecodeError"
    report = HookHealthService(Database(db_path), repo_root=Path(__file__).parents[1]).report(
        hours=24, hook_log=hook_log
    )
    assert report["errors"]["recent_count"] == 1
    assert any(issue["code"] == "hook_fail_open_errors" for issue in report["issues"])


def test_structured_hook_error_does_not_copy_validation_input(tmp_path: Path) -> None:
    hook_log = tmp_path / "redacted-hook.log"
    previous = os.environ.get("CODEMEMORY_HOOK_LOG")
    os.environ["CODEMEMORY_HOOK_LOG"] = str(hook_log)
    try:
        try:
            raise ValueError("validation failed\ninput_value=SECRET_PROMPT\nfield=prompt")
        except ValueError as exc:
            append_hook_error(exc, stage="handle")
    finally:
        if previous is None:
            os.environ.pop("CODEMEMORY_HOOK_LOG", None)
        else:
            os.environ["CODEMEMORY_HOOK_LOG"] = previous
    text = hook_log.read_text(encoding="utf-8")
    assert "SECRET_PROMPT" not in text
    assert "validation failed" in text


def test_codex_hook_requests_one_bounded_llm_maintenance_pass(tmp_path: Path) -> None:
    db_path = tmp_path / "stop-hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    payload = {
        "hook_event_name": "Stop",
        "session_id": "stop-session-1",
        "turn_id": "stop-turn-1",
        "last_assistant_message": "Implemented Assets/Scripts/Npc.cs and compiled.",
        "cwd": "D:/Project_J",
    }
    env = dict(os.environ)
    env["CODEMEMORY_DB"] = str(db_path)
    env["CODEMEMORY_MAINTENANCE_DIR"] = str(tmp_path / "maintenance")
    env.pop("CODEMEMORY_PROJECT_ID", None)
    tool_result = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(
            {
                **payload,
                "hook_event_name": "PostToolUse",
                "tool_use_id": "stop-tool-1",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "apply_patch <<PATCH\n*** Update File: Assets/Scripts/Npc.cs\n@@\n*** End Patch\nPATCH"
                },
                "tool_response": {"exitCode": 0, "output": "patched"},
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert json.loads(tool_result.stdout) == {}
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
    assert output["decision"] == "block"
    assert output["reason"].startswith("[CODEMEMORY_MAINTENANCE]")
    repository = MemoryRepository(Database(db_path))
    pending = repository.list_agent_memory_maintenance()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    learn_path = next((tmp_path / "maintenance").glob("*.learn.json"))
    request_path = next((tmp_path / "maintenance").glob("*.request.json"))
    learn_payload = json.loads(learn_path.read_text(encoding="utf-8"))
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    edit_event = next(
        event for event in request_payload["events"] if event["event_type"] == "file_edit"
    )
    learn_payload["notes"] = [
        {
            "kind": "route_observation",
            "statement": "NPC coding work routes through Assets/Scripts/Npc.cs.",
            "aliases": ["NPC route"],
            "bindings": [
                {
                    "role": "modified_file",
                    "path": "Assets/Scripts/Npc.cs",
                    "symbol": None,
                    "qualified_symbol": None,
                    "evidence": [edit_event["event_id"]],
                }
            ],
            "evidence_event_ids": [edit_event["event_id"]],
            "confidence": 0.7,
            "uncertainty": "Historical route observation; verify current source.",
            "relation_hints": [],
        }
    ]
    assert learn_payload["project_id"] == "project_j"
    assert learn_payload["task_id"].startswith("codex-hook-task:")
    learned = AgentMemoryCycleService(repository).learn(
        CycleLearnRequest.model_validate(learn_payload)
    )
    assert learned["extraction"]["status"] == "extracted"
    assert learned["extraction"]["candidate_count"] == 1
    assert learned["consolidation"]["created"] == 1
    assert learned["maintenance"]["status"] == "completed"
    assert learned["maintenance"]["candidate_count"] == 1
    report = HookHealthService(Database(db_path), repo_root=Path(__file__).parents[1]).report(
        hours=24, hook_log=tmp_path / "missing-hook.log"
    )
    assert report["maintenance"]["counts"]["completed"] == 1
    assert report["memory_projection"]["agent_llm_extractions"] == 1
    assert report["memory_projection"]["candidates"] == 1
    assert report["memory_projection"]["cards_updated"] == 1
    assert report["project_mapping"]["mismatched_event_count"] == 0

    follow_up_prompt = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(
            {
                **payload,
                "hook_event_name": "UserPromptSubmit",
                "turn_id": "follow-up-prompt",
                "prompt": "Summarize the result briefly.",
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert "hookSpecificOutput" in json.loads(follow_up_prompt.stdout)
    follow_up_stop = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(
            {
                **payload,
                "turn_id": "follow-up-stop",
                "last_assistant_message": "Done.",
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert json.loads(follow_up_stop.stdout) == {}
    assert len(repository.list_agent_memory_maintenance()) == 1


def test_unfinished_hook_maintenance_is_failed_then_retried(tmp_path: Path) -> None:
    db_path = tmp_path / "retry-hook.sqlite3"
    hook = Path(__file__).parents[1] / "integrations" / "codex" / "codememory_hook.py"
    payload = {
        "hook_event_name": "Stop",
        "session_id": "retry-session-1",
        "turn_id": "retry-turn-1",
        "last_assistant_message": "Implemented Assets/Scripts/Npc.cs and compiled.",
        "cwd": "D:/P4Workspace/client/mainline",
    }
    env = dict(os.environ)
    env["CODEMEMORY_DB"] = str(db_path)
    env["CODEMEMORY_MAINTENANCE_DIR"] = str(tmp_path / "maintenance-retry")
    env["CODEMEMORY_MAINTENANCE_MAX_ATTEMPTS"] = "2"
    first = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert json.loads(first.stdout)["decision"] == "block"
    continuation = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({**payload, "stop_hook_active": True}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert json.loads(continuation.stdout) == {}
    repository = MemoryRepository(Database(db_path))
    assert repository.list_agent_memory_maintenance()[0]["status"] == "failed"
    retry = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert json.loads(retry.stdout)["decision"] == "block"
    retried = repository.list_agent_memory_maintenance()[0]
    assert retried["status"] == "pending"
    assert retried["attempts"] == 2
    assert retried["retry_allowed"] is False


def test_projection_reset_clears_completed_maintenance_but_keeps_events(tmp_path: Path) -> None:
    database = Database(tmp_path / "maintenance-reset.sqlite3")
    repository = MemoryRepository(database)
    service = AgentMemoryCycleService(repository)
    base = _base()
    opened = service.open(CycleOpenRequest(**base, prompt="NPC route", turn_id="reset-prompt"))
    prepared = service.prepare(CycleBaseRequest(**base))
    service.request_maintenance(
        CycleBaseRequest(**base),
        input_hash=prepared["input_hash"],
        turn_id="reset-stop",
    )
    events_before = len(repository.timeline(opened["task_id"], session_id=opened["session_id"]))
    assert repository.agent_memory_maintenance_count()["pending"] == 1
    result = ProjectionMaintenance(database).reset()
    assert result["deleted"]["agent_memory_maintenance"] == 1
    assert repository.agent_memory_maintenance_count()["pending"] == 0
    assert (
        len(repository.timeline(opened["task_id"], session_id=opened["session_id"]))
        == events_before
    )
