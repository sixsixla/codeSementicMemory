from __future__ import annotations

from pathlib import Path

from codememory.ingest.service import IngestService, replay_jsonl
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def test_fixture_replay_is_deterministic(tmp_path):
    fixture = Path(__file__).parents[2] / "fixtures" / "route-success.jsonl"
    service = IngestService(MemoryRepository(Database(tmp_path / "memory.sqlite3")))
    first = replay_jsonl(fixture, service)
    second = replay_jsonl(fixture, service)
    assert first.accepted == 12
    assert first.duplicates == 0
    assert second.accepted == 0
    assert second.duplicates == 12
    assert second.errors == []
