from __future__ import annotations

import json
from pathlib import Path

from codememory.cli import validate_schema_file
from codememory.domain.events import EventEnvelope
from codememory.ingest.service import IngestService
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def test_all_fixture_streams_validate_against_event_contract():
    root = Path(__file__).parents[2] / "fixtures"
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    for item in index["fixtures"]:
        result = validate_schema_file(root / item["path"])
        assert result["invalid"] == 0, (item["path"], result["errors"])
        assert result["total"] == item["expected_events"]
        expected = json.loads((root / item["expected_path"]).read_text(encoding="utf-8"))
        assert expected["expected_events"] == result["total"]


def test_backup_contains_a_consistent_copy(tmp_path, event_factory):
    source = Database(tmp_path / "source.sqlite3")
    repo = MemoryRepository(source)
    repo.ingest(EventEnvelope.model_validate(event_factory(event_id="backup-event", seq=1)))
    backup_path = source.backup_to(tmp_path / "backup.sqlite3")
    backup_repo = MemoryRepository(Database(backup_path))
    assert backup_repo.health()["counts"]["events"] == 1
    assert backup_repo.get_event("backup-event")["event_id"] == "backup-event"
    restored_path = Database.restore_from(backup_path, tmp_path / "restored.sqlite3")
    assert MemoryRepository(Database(restored_path)).health()["counts"]["events"] == 1
    exported = repo.export_events(task_id="pytest-task")
    assert len(exported) == 1
    assert "ingested_at" not in exported[0]
    assert EventEnvelope.model_validate(exported[0]).event_id == "backup-event"


def test_ten_thousand_events_replay_integrity(tmp_path, event_factory):
    """The acceptance-scale stream stays lossless; timing is intentionally not asserted."""

    repo = MemoryRepository(Database(tmp_path / "large.sqlite3"))
    service = IngestService(repo)
    events = [
        EventEnvelope.model_validate(
            event_factory(
                event_id=f"large-{index}",
                seq=index,
                session_id="large-session",
                payload={"text": f"route observation {index}"},
            )
        )
        for index in range(10_000)
    ]
    service.ingest_many(events)
    health = repo.health()
    assert health["counts"]["events"] == 10_000
    assert health["outbox"]["pending"] == 10_000
    assert health["integrity_check"] == "ok"
