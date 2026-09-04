from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from codememory.domain.events import EVENT_SCHEMA_VERSION


@pytest.fixture
def event_factory():
    def make(
        *,
        event_id: str = "evt-test-1",
        event_type: str = "user_message",
        seq: int = 1,
        session_id: str | None = "session-test",
        parent_event_id: str | None = None,
        payload: dict[str, Any] | None = None,
        source: str = "agent",
    ) -> dict[str, Any]:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "external_event_id": f"external-{event_id}",
            "event_type": event_type,
            "occurred_at": (
                datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc) + timedelta(seconds=seq)
            ).isoformat(),
            "producer": {
                "agent_id": "pytest-agent",
                "adapter": "pytest",
                "adapter_version": "1",
            },
            "project_id": "pytest-project",
            "task_id": "pytest-task",
            "session_id": session_id,
            "seq": seq,
            "parent_event_id": parent_event_id,
            "context": {"repo_id": "pytest-repo", "root_path": str("D:/pytest/repo")},
            "payload": payload or {"text": "hello coding memory"},
            "artifacts": [],
            "redaction": {"applied": False, "ruleset_version": "builtin-v1", "fields": []},
            "source": source,
            "completeness": "full",
        }

    return make
