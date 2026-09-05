from __future__ import annotations

from fastapi.testclient import TestClient

from codememory.api import create_app


def test_agent_bridge_http_lifecycle(tmp_path):
    client = TestClient(create_app(tmp_path / "api.sqlite3"))
    start_payload = {
        "project_id": "project_j",
        "task_id": "task-api-bridge",
        "session_id": "session-api-bridge",
        "title": "API bridge smoke",
        "intent": "NPC 交互结束后打开分享面板",
        "context": {"repo_id": "project-j-repo"},
        "occurred_at": "2026-09-05T04:20:00Z",
    }
    started = client.post("/v1/agent/start", json=start_payload)
    repeated = client.post("/v1/agent/start", json=start_payload)
    assert started.status_code == 200
    assert started.json()["event"]["status"] == "accepted"
    assert repeated.json()["event"]["status"] == "duplicate"

    captured = client.post(
        "/v1/agent/capture",
        json={
            "project_id": "project_j",
            "task_id": "task-api-bridge",
            "session_id": "session-api-bridge",
            "capture_id": "capture-api-1",
            "explored_files": ["Assets/Script/Npc/YeQuNpcInteract.cs"],
            "modified_files": ["Assets/Script/UI/CommonSharePanel.cs"],
            "symbols": ["YeQuNpcInteract", "CommonSharePanel"],
            "validations": [{"command": "compile", "status": "passed"}],
            "summary": "编译通过",
            "outcome": "success",
            "captured_at": "2026-09-05T04:21:00Z",
        },
    )
    assert captured.status_code == 200
    assert captured.json()["accepted"] >= 1

    finished = client.post(
        "/v1/agent/finish",
        json={
            "project_id": "project_j",
            "task_id": "task-api-bridge",
            "session_id": "session-api-bridge",
            "summary": "任务完成",
            "outcome": "success",
            "occurred_at": "2026-09-05T04:22:00Z",
        },
    )
    assert finished.status_code == 200
    assert finished.json()["extraction"]["status"] == "extracted"

    query = client.post(
        "/v1/agent/query",
        json={
            "query": "CommonSharePanel",
            "project_id": "project_j",
            "task_id": "task-api-bridge",
        },
    )
    assert query.status_code == 200
    assert query.json()["cards"] or query.json()["events"]
    assert query.json()["retrieval_mode"] == "route"
