from __future__ import annotations

import pytest

from codememory.domain.events import EventEnvelope
from codememory.ingest.service import IngestService
from codememory.storage.database import Database
from codememory.storage.repository import ConflictError, MemoryRepository


def make_repo(tmp_path):
    return MemoryRepository(Database(tmp_path / "memory.sqlite3"))


def test_migration_is_repeatable_and_wal(tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    db.initialize()
    db.initialize()
    assert db.journal_mode() == "wal"
    assert db.migration_version() == "0009_agent_memory_cycle.sql"
    assert db.integrity_check() == "ok"


def test_ingest_is_idempotent_and_redacts(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    service = IngestService(repo)
    event = EventEnvelope.model_validate(
        event_factory(payload={"text": "implement route", "api_key": "sk-secret-abcdefghijklmnop"})
    )
    first = service.ingest(event)
    second = service.ingest(event)
    assert first.status == "accepted"
    assert second.status == "duplicate"
    assert second.outbox_job_id == first.outbox_job_id
    assert repo.health()["counts"]["events"] == 1
    assert repo.health()["counts"]["artifacts"] == 0
    stored = repo.get_event(event.event_id)
    assert stored is not None
    assert stored["payload"]["api_key"] == "[REDACTED]"
    assert repo.outbox_count()["pending"] == 1

    changed = event.model_copy(update={"payload": {"text": "different"}})
    with pytest.raises(ConflictError):
        service.ingest(changed)


def test_redaction_covers_context_metadata(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    raw = event_factory(payload={"text": "context secret"})
    raw["context"]["debug_token"] = "token=abcdefghijklmnop"
    event = EventEnvelope.model_validate(raw)
    service = IngestService(repo)
    service.ingest(event)
    stored = repo.get_event(event.event_id)
    assert stored is not None
    assert stored["context"]["debug_token"] == "token=[REDACTED]"
    assert "context.debug_token" in stored["redaction"]["fields"]


def test_redaction_covers_opaque_nested_secret_keys(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    raw = event_factory(
        payload={
            "tool": "http_client",
            "arguments": {
                "api_key": "opaque-provider-value",
                "nested": {"client-secret": "another-opaque-value"},
                "token_count": 12,
            },
        }
    )
    event = EventEnvelope.model_validate(raw)
    IngestService(repo).ingest(event)
    stored = repo.get_event(event.event_id)
    assert stored is not None
    assert stored["payload"]["arguments"]["api_key"] == "[REDACTED]"
    assert stored["payload"]["arguments"]["nested"]["client-secret"] == "[REDACTED]"
    assert stored["payload"]["arguments"]["token_count"] == 12
    assert "payload.arguments.api_key" in stored["redaction"]["fields"]
    assert "payload.arguments.nested.client-secret" in stored["redaction"]["fields"]


def test_outbox_claim_complete_and_retry(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    for index in range(3):
        repo.ingest(EventEnvelope.model_validate(event_factory(event_id=f"evt-{index}", seq=index)))
    claimed = repo.claim_outbox(limit=2, lease_seconds=30)
    assert len(claimed) == 2
    assert all(job.status == "processing" and job.attempts == 1 for job in claimed)
    assert repo.complete_outbox(claimed[0].job_id) is True
    assert repo.fail_outbox(claimed[1].job_id, "temporary", dead=False) is True
    counts = repo.outbox_count()
    assert counts["completed"] == 1
    assert counts["retry"] == 1
    assert counts["pending"] == 1


def test_outbox_moves_to_dead_after_max_attempts(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    repo.ingest(EventEnvelope.model_validate(event_factory(event_id="dead-event", seq=1)))
    for attempt in range(8):
        claimed = repo.claim_outbox(limit=1, lease_seconds=30)
        assert len(claimed) == 1
        assert claimed[0].attempts == attempt + 1
        assert repo.fail_outbox(claimed[0].job_id, f"failure-{attempt}", retry_delay_seconds=0)
    assert repo.outbox_count()["dead"] == 1


def test_expired_outbox_lease_is_recovered(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    repo.ingest(EventEnvelope.model_validate(event_factory(event_id="lease-event", seq=1)))
    first = repo.claim_outbox(limit=1, lease_seconds=1)[0]
    with repo.db.transaction() as conn:
        conn.execute(
            "UPDATE outbox SET lease_until='2000-01-01T00:00:00+00:00' WHERE job_id=?",
            (first.job_id,),
        )
    recovered = repo.claim_outbox(limit=1, lease_seconds=30)
    assert len(recovered) == 1
    assert recovered[0].job_id == first.job_id
    assert recovered[0].attempts == 2


def test_late_parent_event_is_allowed_and_searchable(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    child = EventEnvelope.model_validate(
        event_factory(
            event_id="child", seq=2, parent_event_id="parent", payload={"text": "share panel"}
        )
    )
    parent = EventEnvelope.model_validate(
        event_factory(event_id="parent", seq=1, payload={"text": "NPC interaction"})
    )
    assert repo.ingest(child).status == "accepted"
    assert repo.health()["unresolved_parent_links"] == 1
    assert repo.ingest(parent).status == "accepted"
    assert repo.health()["unresolved_parent_links"] == 0
    timeline = repo.timeline("pytest-task", session_id="session-test")
    assert [item["event_id"] for item in timeline] == ["parent", "child"]
    results = repo.search("share")
    assert any(item["event_id"] == "child" for item in results)
    # FTS syntax errors from punctuation are treated as literal searches.
    assert repo.search("share/path.py") == []


def test_artifact_metadata_and_event_link_are_persisted(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    raw = event_factory(event_id="artifact-event", payload={"path": "src/main.py"})
    raw["artifacts"] = [
        {
            "kind": "source_file",
            "path": "src/main.py",
            "mime_type": "text/plain",
            "metadata": {"role": "entrypoint"},
        }
    ]
    event = EventEnvelope.model_validate(raw)
    repo.ingest(event)
    with repo.db.connection() as conn:
        artifact = conn.execute("SELECT * FROM artifacts").fetchone()
        link = conn.execute("SELECT * FROM event_artifacts").fetchone()
    assert artifact is not None
    assert artifact["path"] == "src/main.py"
    assert link["event_id"] == "artifact-event"


def test_artifact_content_is_hashed_and_truncated(tmp_path, event_factory):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db, artifact_max_bytes=8)
    raw = event_factory(event_id="content-event", payload={"text": "content"})
    raw["artifacts"] = [
        {
            "kind": "diff",
            "path": "change.diff",
            "content": "0123456789abcdef",
            "mime_type": "text/plain",
        }
    ]
    repo.ingest(EventEnvelope.model_validate(raw))
    with db.connection() as conn:
        artifact = conn.execute("SELECT * FROM artifacts WHERE path='change.diff'").fetchone()
    assert artifact["size_bytes"] == 16
    assert artifact["content_excerpt"] == "01234567"
    assert artifact["truncated"] == 1
    assert artifact["metadata_json"]
    stored_event = repo.get_event("content-event")
    assert stored_event["artifacts"][0]["content"] is None


def test_entity_identity_cannot_cross_projects(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    first = event_factory(event_id="entity-a", seq=1)
    repo.ingest(EventEnvelope.model_validate(first))
    second = event_factory(event_id="entity-b", seq=2)
    second["project_id"] = "another-project"
    with pytest.raises(ConflictError):
        repo.ingest(EventEnvelope.model_validate(second))


def test_external_and_sequence_identities_are_idempotent(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    first = EventEnvelope.model_validate(event_factory(event_id="identity-a", seq=3))
    repo.ingest(first)
    by_external = first.model_copy(update={"event_id": "identity-b"})
    with pytest.raises(ConflictError):
        repo.ingest(by_external)
    by_seq = first.model_copy(update={"event_id": "identity-c", "external_event_id": "external-c"})
    with pytest.raises(ConflictError):
        repo.ingest(by_seq)


def test_search_projection_can_be_rebuilt(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    repo.ingest(
        EventEnvelope.model_validate(
            event_factory(event_id="fts-event", payload={"text": "rebuild marker"})
        )
    )
    with repo.db.transaction() as conn:
        conn.execute("DELETE FROM event_fts")
    assert repo.search("rebuild") == []
    assert repo.rebuild_search_index() == 1
    assert repo.search("rebuild")[0]["event_id"] == "fts-event"


def test_replay_keeps_entity_recency_at_source_time(tmp_path, event_factory):
    repo = make_repo(tmp_path)
    older = EventEnvelope.model_validate(event_factory(event_id="old-event", seq=1))
    newer = EventEnvelope.model_validate(event_factory(event_id="new-event", seq=20))
    repo.ingest(older)
    repo.ingest(newer)
    task = repo.list_tasks()[0]
    assert task["updated_at"] == newer.occurred_at.isoformat()
    assert repo.list_tasks(updated_since="2026-09-02T08:00:20+00:00")[0]["task_id"] == "pytest-task"
    assert repo.list_tasks(min_events=3) == []
