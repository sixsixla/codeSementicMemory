from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codememory.api import create_app
from codememory.consolidation.service import ConsolidationService
from codememory.consolidation.store import CardStore
from codememory.extraction.models import EXTRACTION_SCHEMA_VERSION
from codememory.extraction.service import ExtractionService, FixedProvider
from codememory.extraction.store import ExtractionStore
from codememory.history.codex import CodexHistoryImporter
from codememory.ingest.service import IngestService, replay_jsonl
from codememory.maintenance import ProjectionMaintenance
from codememory.quality.classifier import classify_event, review_candidate
from codememory.quality.service import QualityService
from codememory.quality.store import QualityStore
from codememory.quality.project_scope import logical_project_id
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def _fixture_path() -> Path:
    return Path(__file__).parents[2] / "fixtures" / "route-success.jsonl"


def test_event_and_candidate_quality_are_deterministic(event_factory, tmp_path):
    ack = event_factory(event_id="ack", payload={"text": "按上面计划执行开发"})
    quality = classify_event(ack)
    assert quality.decision == "quarantine"
    assert quality.role == "noise"
    route = event_factory(
        event_id="intent",
        payload={"text": "修复 NPC 对话后的分享面板，修改 Assets/Scripts/Share.cs"},
    )
    code = event_factory(
        event_id="edit",
        event_type="file_edit",
        seq=2,
        payload={"path": "Assets/Scripts/Share.cs", "symbols": ["Share.Open"]},
    )
    candidate = {
        "candidate_id": "candidate-route",
        "project_id": "pytest-project",
        "task_id": "pytest-task",
        "kind": "route_observation",
        "statement": "Coding route observed: 修复 NPC 对话后的分享面板",
        "aliases": ["NPC分享"],
        "bindings": [
            {
                "role": "modified_file",
                "path": "Assets/Scripts/Share.cs",
                "evidence": ["edit"],
            }
        ],
        "evidence_event_ids": ["intent", "edit"],
        "confidence": 0.8,
    }
    review = review_candidate(candidate, {"intent": route, "edit": code})
    assert review.decision == "accepted"
    assert review.dimensions["valid_bindings"] == 1
    bad = dict(candidate)
    bad["candidate_id"] = "candidate-noise"
    bad["statement"] = "Coding route observed: 同意"
    bad["bindings"] = []
    assert review_candidate(bad, {"intent": ack}).decision == "quarantine"
    external = event_factory(
        event_id="external-project-j",
        payload={"text": "请参考 Project_J 的 NPC 归属逻辑"},
    )
    external["context"] = {"root_path": "D:/Tool/codeSementicMemory"}
    mismatch = classify_event(external)
    assert mismatch.dimensions["scope_mismatch"] is True
    assert mismatch.decision == "review"
    assert any("scope_mismatch" in reason for reason in mismatch.reasons)
    mismatch_candidate = dict(candidate)
    mismatch_candidate["candidate_id"] = "candidate-scope-mismatch"
    mismatch_candidate["evidence_event_ids"] = ["external-project-j", "edit"]
    reviewed = review_candidate(
        mismatch_candidate,
        {"external-project-j": external, "edit": code},
    )
    assert reviewed.decision == "review"
    assert reviewed.dimensions["scope_mismatch"] is True

    forged_modified = dict(candidate)
    forged_modified["candidate_id"] = "candidate-forged-modified"
    forged_modified["evidence_event_ids"] = ["intent"]
    forged_modified["bindings"] = [
        {
            "role": "modified_file",
            "path": "Assets/Scripts/Share.cs",
            "evidence": ["intent"],
        }
    ]
    forged_review = review_candidate(forged_modified, {"intent": route})
    assert forged_review.decision == "quarantine"
    assert forged_review.dimensions["unverified_modified_bindings"] == 1
    assert any("file_edit/vcs_change" in reason for reason in forged_review.reasons)


def test_quality_replay_is_idempotent_and_preserves_events(tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db)
    ingest = IngestService(repo)
    replay_jsonl(_fixture_path(), ingest, strict=True)
    before = repo.health()["counts"]["events"]
    quality = QualityService(repo)
    dry = quality.replay(task_id="task-npc-share", write=False)
    written = quality.replay(task_id="task-npc-share", write=True)
    repeated = quality.replay(task_id="task-npc-share", write=True)
    assert dry.status == written.status == repeated.status == "succeeded"
    assert dry.input_hash == written.input_hash == repeated.input_hash
    assert written.event_count == 12
    assert written.candidate_count == 0
    assert repo.health()["counts"]["events"] == before
    report = quality.report(task_id="task-npc-share")
    assert report["events"]["evaluated"] == 12
    assert report["events"]["unreviewed"] == 0
    assert len(quality.store.latest_runs()) >= 3


def test_quarantined_candidate_cannot_create_card(tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db)
    replay_jsonl(_fixture_path(), IngestService(repo), strict=True)

    def provider(context):
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
                    "candidate_id": "ack-only-route",
                    "kind": "route_observation",
                    "statement": "Coding route observed: 按上面计划执行开发",
                    "aliases": [],
                    "bindings": [],
                    "evidence_event_ids": [context.event_ids[1]],
                    "confidence": 0.9,
                    "uncertainty": "ack only",
                }
            ],
        }

    extraction = ExtractionService(
        repo,
        provider=FixedProvider(provider),
        store=ExtractionStore(db),
    )
    assert extraction.extract_task("task-npc-share").status == "extracted"
    candidate_store = ExtractionStore(db)
    assert candidate_store.list_candidates(task_id="task-npc-share", include_quarantine=False) == []
    assert candidate_store.list_candidates(task_id="task-npc-share", include_quarantine=True)
    assert candidate_store.search(
        "按上面计划执行开发", task_id="task-npc-share", include_quarantine=False
    ) == []
    assert candidate_store.search(
        "按上面计划执行开发", task_id="task-npc-share", include_quarantine=True
    )
    result = ConsolidationService(repo).consolidate(task_id="task-npc-share")
    assert result.rejected == 1
    assert result.created == 0
    assert not result.items[0].card_id
    assert QualityService(repo).candidate_quality("ack-only-route")["decision"] == "quarantine"


def test_project_j_roots_resolve_to_one_logical_scope(event_factory, tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db)
    first = event_factory(event_id="project-j-1", payload={"text": "修改 Project_J 代码"})
    first["project_id"] = "raw-project-d"
    first["task_id"] = "project-j-task-d"
    first["session_id"] = "session-d"
    first["context"] = {"repo_id": "repo-d", "root_path": "D:/workspace/Project_J"}
    second = event_factory(event_id="project-j-2", seq=2, payload={"text": "修复 Project_J 逻辑"})
    second["project_id"] = "raw-project-e"
    second["task_id"] = "project-j-task-e"
    second["session_id"] = "session-e"
    second["context"] = {"repo_id": "repo-e", "root_path": "E:/release/Project_J/"}
    IngestService(repo).ingest_many([first, second])
    third = event_factory(event_id="project-j-3", seq=3, payload={"text": "修复 Project_J 主工程逻辑"})
    third["project_id"] = "raw-project-mainline"
    third["task_id"] = "project-j-task-mainline"
    third["session_id"] = "session-mainline"
    third["context"] = {"root_path": "D:/P4Workspace/client/mainline"}
    IngestService(repo).ingest(third)
    quality = QualityService(repo)
    logical_id = logical_project_id("name:project_j")
    quality.register_project_alias(
        raw_project_id="raw-project-mainline",
        logical_project_id=logical_id,
        alias_value="D:/P4Workspace/client/mainline -> Project_J",
        evidence={"source": "reviewed test alias"},
    )
    projects = quality.store.list_logical_projects()
    project_j = [item for item in projects if item["identity_key"] == "name:project_j"]
    assert len(project_j) == 1
    assert {item["raw_project_id"] for item in project_j[0]["aliases"]} == {
        "raw-project-d",
        "raw-project-e",
        "raw-project-mainline",
    }
    assert quality.store.logical_project_for_raw("raw-project-mainline")["logical_project_id"] == logical_id
    assert set(quality.store.raw_project_ids_for_logical(logical_id)) == {
        "raw-project-d",
        "raw-project-e",
        "raw-project-mainline",
    }
    replay = quality.replay(logical_project_id_value=logical_id, write=False)
    assert replay.event_count == 3
    assert replay.logical_project_count == 1


def test_scope_mismatch_is_persisted_and_reported(event_factory, tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db)
    event = event_factory(
        event_id="scope-mismatch-event",
        payload={"text": "参考 Project_J 的实现，修复本项目文档"},
    )
    event["project_id"] = "codememory-project"
    event["task_id"] = "codememory-task"
    event["session_id"] = "codememory-session"
    event["context"] = {"root_path": "D:/Tool/codeSementicMemory"}
    IngestService(repo).ingest(event)
    report = QualityService(repo).replay(task_id="codememory-task", write=True)
    assert report.status == "succeeded"
    quality_report = QualityService(repo).report(task_id="codememory-task")
    assert quality_report["events"]["scope_mismatches"] == 1


def test_quality_projection_failure_does_not_rollback_ingest(event_factory, tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db)

    class BrokenQuality:
        def evaluate_event_id(self, event_id):
            raise RuntimeError(f"synthetic classifier failure: {event_id}")

        def evaluate_events(self, events):
            raise RuntimeError("synthetic batch classifier failure")

    service = IngestService(repo, quality_service=BrokenQuality())
    event = event_factory(event_id="quality-failure-event")
    result = service.ingest(event)
    assert result.status == "accepted"
    assert repo.health(check_integrity=False)["counts"]["events"] == 1


def test_quality_api_reports_and_replays(tmp_path):
    app = create_app(tmp_path / "memory.sqlite3")
    client = TestClient(app)
    history = Path(__file__).parents[2] / "fixtures" / "codex-history" / "selected-threads.json"
    summary = CodexHistoryImporter(app.state.ingest_service).import_file(history)
    task_id = summary.tasks[0]
    response = client.get("/v1/quality/report", params={"task_id": task_id})
    assert response.status_code == 200
    assert response.json()["events"]["evaluated"] > 0
    replay = client.post(
        "/v1/quality/replay",
        json={"task_id": task_id, "write": True},
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "succeeded"
    projects = client.get("/v1/quality/projects")
    assert projects.status_code == 200
    assert projects.json()["projects"]


def test_interrupted_quality_run_is_recovered_on_next_start(tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    store = QualityStore(db)
    stale_id = store.start_run(
        scope="all",
        input_hash="stale-input",
        classifier_version="test-v1",
        mode="write",
    )
    # Simulate a process terminated after creating the audit row.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE quality_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE run_id=?",
            (stale_id,),
        )
    assert store.recover_stale_runs(max_age_seconds=1) == 1
    run = [item for item in store.latest_runs() if item["run_id"] == stale_id][0]
    assert run["status"] == "failed"
    assert "stale running quality run recovered" in run["error"]


def test_quarantine_is_hidden_from_default_card_and_graph_views(tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db)
    replay_jsonl(_fixture_path(), IngestService(repo), strict=True)
    ExtractionService(repo, store=ExtractionStore(db)).extract_task("task-npc-share")
    ConsolidationService(repo).consolidate(task_id="task-npc-share")
    cards = CardStore(db).list_cards(task_id="task-npc-share", include_quarantine=True)
    assert cards
    target = cards[0]
    # Pick the card's first evidence candidate and turn its derived review into
    # an explicit quarantine, simulating a later policy upgrade/re-evaluation.
    detail = CardStore(db).get_card(target["card_id"])
    evidence_candidate = detail["versions"][0]["evidence"][0]["candidate_id"]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE candidate_quality_reviews SET decision='quarantine' WHERE candidate_id=?",
            (evidence_candidate,),
        )
    visible = CardStore(db).list_cards(task_id="task-npc-share")
    all_cards = CardStore(db).list_cards(task_id="task-npc-share", include_quarantine=True)
    assert len(visible) == len(all_cards) - 1
    assert target["card_id"] not in {item["card_id"] for item in visible}
    assert target["card_id"] in {item["card_id"] for item in all_cards}
    graph = ExtractionStore(db).graph(task_id="task-npc-share", limit=100)
    graph = CardStore(db).extend_graph(graph, task_id="task-npc-share", limit=100)
    assert f"card:{target['card_id']}" not in {node["id"] for node in graph["nodes"]}
    debug_graph = ExtractionStore(db).graph(
        task_id="task-npc-share", limit=100, include_quarantine=True
    )
    debug_graph = CardStore(db).extend_graph(
        debug_graph, task_id="task-npc-share", limit=100, include_quarantine=True
    )
    assert f"card:{target['card_id']}" in {node["id"] for node in debug_graph["nodes"]}


def test_projection_reset_clears_quality_projections_but_preserves_source(tmp_path):
    db = Database(tmp_path / "memory.sqlite3")
    repo = MemoryRepository(db)
    replay_jsonl(_fixture_path(), IngestService(repo), strict=True)
    ExtractionService(repo, store=ExtractionStore(db)).extract_task("task-npc-share")
    QualityService(repo).replay(task_id="task-npc-share", write=True)
    before = repo.health()["counts"]
    result = ProjectionMaintenance(db).reset()
    after = repo.health()["counts"]
    assert result["source_tables_preserved"] is True
    assert after["events"] == before["events"] == 12
    assert after["artifacts"] == before["artifacts"]
    assert after["memory_candidates"] == 0
    assert after["memory_cards"] == 0
    assert after["event_quality_evaluations"] == 0
    assert after["candidate_quality_reviews"] == 0
    assert after["project_aliases"] == 0
    assert after["logical_projects"] == 0
    assert after["quality_runs"] == 0
