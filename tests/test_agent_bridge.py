from __future__ import annotations

from datetime import datetime, timezone

import pytest

from codememory.agent_bridge import (
    AgentBridgeService,
    CaptureRequest,
    FinishRequest,
    MemoryQueryRequest,
    SessionStartRequest,
)
from codememory.storage.database import Database
from codememory.storage.repository import ConflictError, MemoryRepository


def _bridge(tmp_path):
    repository = MemoryRepository(Database(tmp_path / "agent-bridge.sqlite3"))
    return AgentBridgeService(repository), repository


def _start_request() -> SessionStartRequest:
    return SessionStartRequest(
        project_id="project_j",
        task_id="task-agent-bridge",
        session_id="session-agent-bridge",
        title="Project_J route smoke",
        intent="NPC 交互结束后打开通用分享界面",
        context={
            "repo_id": "project-j-repo",
            "root_path": "D:/P4Workspace/client/mainline",
            "branch": "mainline",
        },
        occurred_at=datetime(2026, 9, 5, 4, 10, tzinfo=timezone.utc),
    )


def _capture_request() -> CaptureRequest:
    return CaptureRequest(
        project_id="project_j",
        task_id="task-agent-bridge",
        session_id="session-agent-bridge",
        capture_id="capture-route-1",
        intent="NPC 交互结束后打开通用分享界面",
        explored_files=["Assets/Script/Npc/YeQuNpcInteract.cs"],
        modified_files=["Assets/Script/UI/CommonSharePanel.cs"],
        symbols=["YeQuNpcInteract", "CommonSharePanel", "ShareManager"],
        validations=[{"command": "compile", "status": "passed"}],
        rejected_candidates=["Assets/Script/UI/OldSharePanel.cs"],
        summary="找到 NPC 交互入口和通用分享面板，编译通过。",
        outcome="success",
        captured_at=datetime(2026, 9, 5, 4, 11, tzinfo=timezone.utc),
    )


def test_manual_lifecycle_is_idempotent_and_extractable(tmp_path):
    bridge, repository = _bridge(tmp_path)
    start = _start_request()

    first_start = bridge.start(start)
    repeated_start = bridge.start(start)
    assert first_start["event"]["status"] == "accepted"
    assert repeated_start["event"]["status"] == "duplicate"

    capture = _capture_request()
    first_capture = bridge.capture(capture)
    repeated_capture = bridge.capture(capture)
    assert first_capture["accepted"] == first_capture["event_count"]
    assert repeated_capture["duplicates"] == repeated_capture["event_count"]

    with pytest.raises(ConflictError):
        bridge.capture(capture.model_copy(update={"summary": "changed evidence"}))

    finished = bridge.finish(
        FinishRequest(
            project_id="project_j",
            task_id="task-agent-bridge",
            session_id="session-agent-bridge",
            summary="任务完成，分享面板路由已确认。",
            outcome="success",
            occurred_at=datetime(2026, 9, 5, 4, 12, tzinfo=timezone.utc),
            extract=True,
            consolidate=True,
        )
    )
    assert finished["event"]["status"] == "accepted"
    assert finished["extraction"]["status"] == "extracted"
    assert finished["consolidation"]["created"] + finished["consolidation"]["merged"] >= 1
    repeated_finish = bridge.finish(
        FinishRequest(
            project_id="project_j",
            task_id="task-agent-bridge",
            session_id="session-agent-bridge",
            summary="任务完成，分享面板路由已确认。",
            outcome="success",
            occurred_at=datetime(2026, 9, 5, 4, 12, tzinfo=timezone.utc),
            extract=True,
            consolidate=True,
        )
    )
    assert repeated_finish["event"]["status"] == "duplicate"

    timeline = repository.timeline("task-agent-bridge", session_id="session-agent-bridge")
    assert [item["seq"] for item in timeline] == sorted(item["seq"] for item in timeline)
    assert timeline[0]["event_type"] == "session_started"
    assert timeline[-1]["event_type"] == "session_ended"

    query = bridge.query(
        MemoryQueryRequest(query="CommonSharePanel", project_id="project_j", task_id="task-agent-bridge")
    )
    assert query["cards"] or query["events"]


def test_manual_capture_marks_summary_completeness(tmp_path):
    bridge, repository = _bridge(tmp_path)
    bridge.start(_start_request())
    bridge.capture(_capture_request())
    events = repository.timeline("task-agent-bridge", session_id="session-agent-bridge")
    captured = [item for item in events if item["payload"].get("capture_id")]
    assert captured
    assert all(item["completeness"] == "summary" for item in captured)
    assert all("reasoning" not in item["payload"] for item in captured)
