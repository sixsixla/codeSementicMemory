from __future__ import annotations

from pathlib import Path

import pytest

from codememory.consolidation.service import ConsolidationService
from codememory.consolidation.store import CardStore
from codememory.domain.events import EventEnvelope
from codememory.extraction.models import EXTRACTION_SCHEMA_VERSION
from codememory.extraction.service import ExtractionService, FixedProvider
from codememory.extraction.store import ExtractionStore
from codememory.history.codex import CodexHistoryImporter
from codememory.ingest.service import IngestService, replay_jsonl
from codememory.maintenance import ProjectionMaintenance
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def _seed(repo: MemoryRepository) -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "route-success.jsonl"
    replay_jsonl(fixture, IngestService(repo), strict=True)
    ExtractionService(repo, store=ExtractionStore(repo.db)).extract_task("task-npc-share")


def test_consolidation_is_idempotent_and_traceable(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed(repo)
    service = ConsolidationService(repo)
    first = service.consolidate(task_id="task-npc-share")
    second = service.consolidate(task_id="task-npc-share")
    assert first.created == 3
    assert second.skipped == 3
    cards = CardStore(repo.db).list_cards(task_id="task-npc-share")
    assert len(cards) == 3
    detail = CardStore(repo.db).get_card(cards[0]["card_id"])
    assert detail and detail["versions"]
    assert detail["versions"][0]["evidence"]
    assert CardStore(repo.db).search("ShareManager")


def test_card_search_can_scope_to_task_and_literal_text(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed(repo)
    service = ConsolidationService(repo)
    service.consolidate(task_id="task-npc-share")
    store = CardStore(repo.db)
    results = store.search("ShareManager", task_id="task-npc-share")
    assert results
    assert all(item["task_id"] == "task-npc-share" for item in results)
    # Punctuation/CJK terms take the deterministic substring fallback when
    # SQLite's unicode61 tokenizer cannot split them as a user expects.
    assert store.search("NPC", task_id="task-npc-share")


def test_material_binding_change_creates_new_version(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed(repo)
    cards = ConsolidationService(repo).consolidate(task_id="task-npc-share")
    route_card = next(
        item
        for item in cards.items
        if item.action == "create"
        and item.card_id
        and item.card_version_id
        and CardStore(repo.db).get_card(item.card_id)["kind"] == "route_observation"
    )
    assert ConsolidationService(repo).promote_card(
        route_card.card_id, reason="snapshot review before follow-up"
    )["status"] == "stable"
    # Add a new visible event so the second extraction has a distinct input
    # hash and evidence id while retaining the original route wording.
    event = EventEnvelope.model_validate(
        {
            "schema_version": "codememory.event.v1",
            "event_id": "evt-route-followup",
            "external_event_id": "followup-1",
            "event_type": "assistant_message",
            "occurred_at": "2026-09-02T08:03:00Z",
            "producer": {"agent_id": "fixture-agent", "adapter": "fixture", "adapter_version": "0.1"},
            "project_id": "demo-game",
            "task_id": "task-npc-share",
            "session_id": "session-001",
            "seq": 12,
            "parent_event_id": "evt-012",
            "context": {"repo_id": "repo-demo", "root_path": "D:/workspace/demo-game"},
            "payload": {"text": "NPC 对话完成后打开通用分享面板。"},
            "source": "replay",
            "completeness": "full",
        }
    )
    IngestService(repo).ingest(event)

    def changed(context):
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
                    "candidate_id": "changed-route-v1",
                    "kind": "route_observation",
                    "statement": "Coding route observed: NPC 对话完成后打开通用分享面板。",
                    "aliases": ["NPC分享", "CommonSharePanel"],
                    "bindings": [
                        {
                            "role": "modified_file",
                            "path": "Assets/Scripts/Gameplay/YeQuNpcInteract.cs",
                            "evidence": [context.event_ids[-1]],
                        },
                        {
                            "role": "feature_integration",
                            "path": "Assets/Scripts/UI/NewSharePanel.cs",
                            "evidence": [context.event_ids[-1]],
                        },
                    ],
                    "evidence_event_ids": [context.event_ids[-1]],
                    "confidence": 0.82,
                    "uncertainty": "new route observation",
                }
            ],
        }

    extraction = ExtractionService(
        repo,
        provider=FixedProvider(changed),
        store=ExtractionStore(repo.db),
    )
    assert extraction.extract_task("task-npc-share").status == "extracted"
    result = ConsolidationService(repo).consolidate(task_id="task-npc-share")
    assert result.new_versions >= 1
    detail = CardStore(repo.db).get_card(route_card.card_id)
    assert detail and len(detail["versions"]) >= 2
    assert any(link["relation_type"] == "supersedes" for link in detail["links"])
    assert detail["versions"][1]["valid_until"] is not None
    assert detail["status"] == "uncertain"


def test_card_lifecycle_requires_valid_transition_and_audit(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed(repo)
    result = ConsolidationService(repo).consolidate(task_id="task-npc-share")
    card_id = next(item.card_id for item in result.items if item.card_id)
    promoted = ConsolidationService(repo).promote_card(card_id, reason="manual review confirmed route")
    assert promoted["status"] == "stable"
    with pytest.raises(ValueError):
        CardStore(repo.db).transition(card_id, "proposed", reason="illegal rollback")
    detail = CardStore(repo.db).get_card(card_id)
    assert detail and detail["lifecycle"][-1]["to_status"] == "stable"




def test_extended_graph_has_no_dangling_card_edges(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed(repo)
    ConsolidationService(repo).consolidate(task_id="task-npc-share")
    graph = CardStore(repo.db).extend_graph(
        ExtractionStore(repo.db).graph(task_id="task-npc-share"), task_id="task-npc-share"
    )
    node_ids = {node["id"] for node in graph["nodes"]}
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in graph["edges"])


def test_bulk_history_import_is_bounded_and_replayable(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    fixture = Path(__file__).parents[2] / "fixtures" / "codex-history" / "selected-threads.json"
    importer = CodexHistoryImporter(IngestService(repo))
    first = importer.import_file(fixture, batch_size=2, max_threads=2)
    second = importer.import_file(fixture, batch_size=2, max_threads=2)
    assert first.threads == 2
    assert first.events >= 2
    assert first.accepted == first.events
    assert second.duplicates == first.events
    assert repo.health()["counts"]["tasks"] == 2


def test_projection_reset_preserves_canonical_source_facts(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed(repo)
    ConsolidationService(repo).consolidate(task_id="task-npc-share")
    before = repo.health()["counts"]
    result = ProjectionMaintenance(repo.db).reset()
    after = repo.health()["counts"]
    assert result["source_tables_preserved"] is True
    assert before["events"] == after["events"]
    assert after["memory_candidates"] == 0
    assert after["memory_cards"] == 0
    assert after["extraction_runs"] == 0


def test_forced_extraction_after_consolidation_preserves_candidate_history(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed(repo)
    store = ExtractionStore(repo.db)
    extraction = ExtractionService(repo, store=store)
    first = extraction.extract_task("task-npc-share")
    assert first.status == "duplicate"
    ConsolidationService(repo).consolidate(task_id="task-npc-share")

    # A provider retry must not delete candidates already referenced by card
    # evidence/decisions.  The same provider ids are therefore written as a
    # new collision-safe generation and linked with ``supersedes``.
    retry = extraction.extract_task("task-npc-share", force=True)
    assert retry.status == "extracted"
    candidates = store.list_candidates(task_id="task-npc-share", limit=20)
    assert len(candidates) >= first.candidate_count * 2
    with repo.db.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM memory_candidate_links WHERE target_type='candidate' "
            "AND relation_type='supersedes' LIMIT 1"
        ).fetchone()
    assert CardStore(repo.db).list_cards(task_id="task-npc-share")
