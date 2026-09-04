from __future__ import annotations

from pathlib import Path

from codememory.extraction.context import ContextAssembler
from codememory.extraction.models import EXTRACTION_SCHEMA_VERSION
from codememory.extraction.service import ExtractionService, FixedProvider
from codememory.extraction.store import ExtractionStore
from codememory.ingest.service import IngestService, replay_jsonl
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def _seed_route(repo: MemoryRepository) -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "route-success.jsonl"
    replay_jsonl(fixture, IngestService(repo), strict=True)


def test_context_hash_and_extraction_are_idempotent(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_route(repo)
    assembler = ContextAssembler(repo)
    first = assembler.assemble("task-npc-share")
    second = assembler.assemble("task-npc-share")
    assert first.input_hash == second.input_hash
    assert first.event_ids == second.event_ids
    service = ExtractionService(repo, assembler=assembler, store=ExtractionStore(repo.db))
    extracted = service.extract_task("task-npc-share")
    duplicate = service.extract_task("task-npc-share")
    assert extracted.status == "extracted"
    assert extracted.candidate_count >= 1
    assert duplicate.status == "duplicate"
    store = ExtractionStore(repo.db)
    assert len(store.list_runs(task_id="task-npc-share")) == 1
    candidates = store.list_candidates(task_id="task-npc-share")
    assert candidates
    known = set(first.event_ids)
    for candidate in candidates:
        assert set(candidate["evidence_event_ids"]) <= known
        for binding in candidate["bindings"]:
            assert binding["evidence"]
            assert set(binding["evidence"]) <= set(candidate["evidence_event_ids"])
    # The literal fallback keeps short Chinese business terms searchable even
    # when SQLite's unicode61 tokenizer does not split the surrounding phrase.
    assert store.search("分享")


def test_context_window_keeps_task_intent_and_latest_validation(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_route(repo)
    context = ContextAssembler(repo, max_events=4, max_chars=20_000).assemble("task-npc-share")
    assert context.truncated is True
    assert len(context.event_ids) <= 4
    # The bounded window must retain both the opening user intent and the
    # closing session/validation evidence; selecting only the first N events
    # would make long coding tasks lose their final outcome.
    assert any(item["event_type"] == "user_message" for item in context.events)
    assert context.events[-1]["event_type"] == "session_ended"


def test_verbose_event_truncation_keeps_context_usable(tmp_path, event_factory):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    service = IngestService(repo)
    raw = event_factory(event_id="verbose-event", payload={"text": "x" * 30_000})
    service.ingest(raw)
    context = ContextAssembler(repo, max_events=10, max_event_chars=200).assemble("pytest-task")
    assert context.truncated is True
    assert context.events
    assert "event text truncated" in context.events[0]["text"]


def test_message_bookkeeping_is_not_promoted_to_natural_language_intent(tmp_path, event_factory):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    service = IngestService(repo)
    service.ingest(
        event_factory(
            event_id="metadata-only-message",
            event_type="user_message",
            payload={"item_id": "compact-turn-0", "raw_item_type": "userMessage"},
        )
    )
    result = ExtractionService(repo, store=ExtractionStore(repo.db)).extract_task("pytest-task")
    assert result.status == "extracted"
    assert result.candidate_count == 0


def test_invalid_provider_output_is_rejected_without_candidates(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_route(repo)

    def invalid(context):
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "extraction_run_id": context.extraction_run_id,
            "project_id": context.project_id,
            "task_id": context.task_id,
            "session_id": context.session_id,
            "source_event_ids": ["not-an-event"],
            "extractor": {"provider": "fixed", "model": "fixture", "prompt_version": "fixed-v1"},
            "candidates": [],
        }

    provider = FixedProvider(invalid)
    service = ExtractionService(repo, provider=provider, store=ExtractionStore(repo.db))
    result = service.extract_task("task-npc-share")
    assert result.status == "failed"
    assert result.error and "unknown" in result.error
    store = ExtractionStore(repo.db)
    run = store.get_run(result.run_id)
    assert run["status"] == "retry"
    assert store.list_candidates(task_id="task-npc-share") == []


def test_forced_failed_retry_keeps_last_successful_candidates(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_route(repo)
    store = ExtractionStore(repo.db)
    first = ExtractionService(repo, store=store).extract_task("task-npc-share")
    previous = store.list_candidates(task_id="task-npc-share")
    assert first.status == "extracted"
    assert previous

    def invalid(context):
        return {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "extraction_run_id": context.extraction_run_id,
            "project_id": context.project_id,
            "task_id": context.task_id,
            "session_id": context.session_id,
            "source_event_ids": list(context.event_ids),
            "extractor": {"provider": "fixed", "model": "fixture", "prompt_version": "fixed-v1"},
            "candidates": [
                {
                    "candidate_id": "bad-evidence",
                    "kind": "decision",
                    "statement": "bad",
                    "evidence_event_ids": ["not-an-event"],
                    "confidence": 0.5,
                    "uncertainty": "invalid test fixture",
                }
            ],
        }

    failed = ExtractionService(
        repo,
        provider=FixedProvider(invalid),
        store=store,
    ).extract_task("task-npc-share", force=True)
    assert failed.status == "failed"
    assert store.list_candidates(task_id="task-npc-share") == previous


def test_outbox_worker_can_extract_without_changing_event_count(tmp_path):
    from codememory.workers.extraction import ExtractionWorker

    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_route(repo)
    before = repo.health()["counts"]["events"]
    summary = ExtractionWorker(repo).run_once(limit=1)
    assert summary.claimed == 1
    assert summary.completed == 1
    assert repo.health()["counts"]["events"] == before
    assert ExtractionStore(repo.db).list_candidates()


def test_provider_runtime_failure_is_retryable_and_dead_lettered(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_route(repo)

    class BrokenProvider:
        provider_name = "broken"
        model_name = "broken-v1"
        prompt_version = "broken-v1"

        def extract(self, context):
            raise RuntimeError("temporary provider outage")

    store = ExtractionStore(repo.db)
    service = ExtractionService(repo, provider=BrokenProvider(), store=store)
    first = service.extract_task("task-npc-share")
    assert first.status == "failed"
    assert "temporary provider outage" in (first.error or "")
    assert store.get_run(first.run_id)["status"] == "retry"

    # Two more explicit attempts reach dead-letter; a normal worker call then
    # stays parked until an operator uses force=True.
    service.extract_task("task-npc-share")
    third = service.extract_task("task-npc-share")
    assert third.status == "failed"
    assert store.get_run(third.run_id)["status"] == "dead"
    parked = service.extract_task("task-npc-share")
    assert parked.status == "dead"
