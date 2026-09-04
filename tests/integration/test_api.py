from __future__ import annotations

from fastapi.testclient import TestClient

from codememory.api import create_app


def test_http_ingest_health_and_timeline(tmp_path, event_factory):
    client = TestClient(create_app(tmp_path / "memory.sqlite3"))
    event = event_factory(event_id="http-1", seq=0, event_type="session_started")
    response = client.post("/v1/events", json=event)
    assert response.status_code == 201
    assert response.json()["status"] == "accepted"
    duplicate = client.post("/v1/events", json=event)
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["counts"]["events"] == 1
    timeline = client.get("/v1/tasks/pytest-task/timeline")
    assert timeline.status_code == 200
    assert timeline.json()["events"][0]["event_id"] == "http-1"


def test_batch_returns_per_event_conflicts(tmp_path, event_factory):
    client = TestClient(create_app(tmp_path / "memory.sqlite3"))
    first = event_factory(event_id="batch-1", seq=1)
    second = event_factory(event_id="batch-2", seq=2)
    response = client.post("/v1/events/batch", json={"events": [first, second]})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 2
    changed = dict(first)
    changed["payload"] = {"text": "conflicting content"}
    response = client.post("/v1/events/batch", json={"events": [changed]})
    assert response.json()["conflicts"] == 1


def test_batch_keeps_valid_records_when_one_is_invalid(tmp_path, event_factory):
    client = TestClient(create_app(tmp_path / "memory.sqlite3"))
    valid = event_factory(event_id="valid-batch", seq=1)
    invalid = {"event_id": "invalid-batch", "seq": -1}
    response = client.post("/v1/events/batch", json={"events": [valid, invalid]})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["invalid"] == 1


def test_http_outbox_lease_lifecycle(tmp_path, event_factory):
    client = TestClient(create_app(tmp_path / "memory.sqlite3"))
    event = event_factory(event_id="outbox-http", seq=1)
    client.post("/v1/events", json=event)
    claimed = client.post("/v1/outbox/claim", json={"limit": 1, "lease_seconds": 30})
    assert claimed.status_code == 200
    job = claimed.json()["jobs"][0]
    completed = client.post(f"/v1/outbox/{job['job_id']}/complete")
    assert completed.json()["completed"] is True
