from __future__ import annotations

from fastapi.testclient import TestClient

from codememory.api import create_app
from codememory.backlog import BacklogService
from codememory.consolidation.store import CardStore
from codememory.extraction.store import ExtractionStore
from codememory.ingest.service import IngestService
from codememory.quality.project_scope import logical_project_id
from codememory.quality.service import QualityService
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


LOGICAL_PROJECT_ID = logical_project_id("name:project_j")


def _seed_task(repo: MemoryRepository, event_factory, *, raw_project: str, task: str) -> None:
    ingest = IngestService(repo)
    session = f"session-{task}"
    root = (
        "D:/P4Workspace/client/mainline/game_client/Project_J"
        if raw_project.endswith("-nested")
        else "D:/P4Workspace/client/mainline"
    )
    for seq, event_type, payload in (
        (0, "user_message", {"text": "NPC 分享入口"}),
        (1, "file_edit", {"path": "Assets/Scripts/Npc.cs", "text": "modified"}),
    ):
        event = event_factory(
            event_id=f"{task}-{seq}",
            event_type=event_type,
            seq=seq,
            session_id=session,
            payload=payload,
        )
        event["project_id"] = raw_project
        event["task_id"] = task
        event["context"] = {"root_path": root, "cwd": root}
        ingest.ingest(event)


def _register_aliases(repo: MemoryRepository, raw_projects: list[str]) -> None:
    quality = QualityService(repo)
    for raw_project in raw_projects:
        quality.register_project_alias(
            raw_project_id=raw_project,
            logical_project_id=LOGICAL_PROJECT_ID,
            alias_value=f"{raw_project}->Project_J",
            alias_type="explicit",
            normalized_root="D:/P4Workspace/client/mainline",
            evidence={"source": "test-reviewed-alias"},
            display_name="Project_J",
        )


def test_logical_scope_retrieves_and_deduplicates_cards_across_raw_projects(
    tmp_path, event_factory
):
    repo = MemoryRepository(Database(tmp_path / "scope.sqlite3"))
    _seed_task(repo, event_factory, raw_project="raw-project-a", task="task-a")
    _seed_task(repo, event_factory, raw_project="raw-project-b-nested", task="task-b")
    _register_aliases(repo, ["raw-project-a", "raw-project-b-nested"])

    backlog = BacklogService(repo)
    plan = backlog.plan(logical_project_id=LOGICAL_PROJECT_ID, limit_tasks=10)
    assert [item.task_id for item in plan] == ["task-a", "task-b"]
    assert all(item.pending_jobs == 2 for item in plan)
    assert all(item.coding_evidence_count == 1 for item in plan)

    dry_run = backlog.process(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit_tasks=10,
        dry_run=True,
    )
    assert dry_run["dry_run"] is True
    assert dry_run["planned_tasks"] == 2

    processed = backlog.process(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit_tasks=10,
        provider="mock",
        consolidate=True,
        complete_outbox=True,
    )
    assert processed.processed_tasks == 2
    assert processed.extracted == 2
    assert processed.failed == 0
    assert processed.outbox_completed == 4
    assert backlog.plan(logical_project_id=LOGICAL_PROJECT_ID) == []
    assert repo.outbox_count()["pending"] == 0

    cards = CardStore(repo.db).list_cards(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit=10,
        retrieval_mode="route",
    )
    assert len(cards) == 1
    assert cards[0]["logical_project_id"] == LOGICAL_PROJECT_ID
    assert set(cards[0]["source_project_ids"]) == {
        "raw-project-a",
        "raw-project-b-nested",
    }
    assert len(ExtractionStore(repo.db).search("NPC", logical_project_id=LOGICAL_PROJECT_ID)) == 2
    events = repo.search("NPC", logical_project_id=LOGICAL_PROJECT_ID, limit=10)
    assert {item["project_id"] for item in events} == {
        "raw-project-a",
        "raw-project-b-nested",
    }


def test_canonical_project_j_raw_id_resolves_to_name_scope(tmp_path, event_factory):
    repo = MemoryRepository(Database(tmp_path / "canonical-project.sqlite3"))
    event = event_factory(
        event_id="canonical-project-start",
        event_type="session_started",
        seq=0,
        session_id="canonical-project-session",
    )
    event["project_id"] = "project_j"
    event["task_id"] = "canonical-project-task"
    event["context"] = {
        "root_path": "D:/P4Workspace/client/mainline",
        "cwd": "D:/P4Workspace/client/mainline",
    }
    IngestService(repo).ingest(event)
    resolved = QualityService(repo).resolve_project(
        project_id="project_j",
        root_path="D:/P4Workspace/client/mainline",
        repo_id="project_j-repo",
    )
    assert resolved["logical_project_id"] == LOGICAL_PROJECT_ID


def test_backlog_plan_recognizes_code_paths_in_message_history(tmp_path, event_factory):
    repo = MemoryRepository(Database(tmp_path / "message-evidence.sqlite3"))
    ingest = IngestService(repo)
    for seq, event_type, text in (
        (0, "user_message", "修复 NPC 分享入口，查看 YeQuNpcInteract.cs"),
        (
            1,
            "assistant_message",
            "已定位 D:/P4Workspace/client/mainline/game_client/Project_J/Assets/Script/YeQuNpcInteract.cs",
        ),
    ):
        event = event_factory(
            event_id=f"message-evidence-{seq}",
            event_type=event_type,
            seq=seq,
            session_id="message-evidence-session",
            payload={"text": text},
        )
        event["project_id"] = "raw-message-project"
        event["task_id"] = "message-evidence-task"
        event["context"] = {
            "root_path": "D:/P4Workspace/client/mainline/game_client/Project_J",
            "cwd": "D:/P4Workspace/client/mainline/game_client/Project_J",
        }
        ingest.ingest(event)
    _register_aliases(repo, ["raw-message-project"])

    plan = BacklogService(repo).plan(logical_project_id=LOGICAL_PROJECT_ID, limit_tasks=10)
    assert len(plan) == 1
    assert plan[0].coding_evidence_count == 1
    assert plan[0].as_dict()["eligible"] is True


def test_logical_scope_filters_graph_tasks_and_preserves_raw_metadata(tmp_path, event_factory):
    repo = MemoryRepository(Database(tmp_path / "graph-scope.sqlite3"))
    _seed_task(repo, event_factory, raw_project="raw-project-a", task="task-a")
    _seed_task(repo, event_factory, raw_project="raw-project-other", task="task-other")
    _register_aliases(repo, ["raw-project-a"])
    BacklogService(repo).process(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit_tasks=10,
        provider="mock",
    )
    graph = ExtractionStore(repo.db).graph(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit=100,
    )
    task_ids = {node.get("task_id") for node in graph["nodes"] if node.get("kind") == "task"}
    assert task_ids == {"task-a"}
    card_graph = CardStore(repo.db).extend_graph(
        graph,
        logical_project_id=LOGICAL_PROJECT_ID,
        limit=100,
    )
    assert card_graph["logical_project_id"] == LOGICAL_PROJECT_ID
    assert all(
        edge["source"] in {node["id"] for node in card_graph["nodes"]}
        for edge in card_graph["edges"]
    )
    assert all(
        edge["target"] in {node["id"] for node in card_graph["nodes"]}
        for edge in card_graph["edges"]
    )


def test_http_and_agent_queries_accept_logical_project_scope(tmp_path, event_factory):
    app = create_app(tmp_path / "api-scope.sqlite3")
    client = TestClient(app)
    _seed_task(app.state.repository, event_factory, raw_project="raw-project-a", task="task-a")
    _seed_task(
        app.state.repository,
        event_factory,
        raw_project="raw-project-b-nested",
        task="task-b",
    )
    _register_aliases(app.state.repository, ["raw-project-a", "raw-project-b-nested"])
    BacklogService(app.state.repository).process(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit_tasks=10,
        provider="mock",
    )

    cards = client.get(
        "/v1/cards/search",
        params={"q": "NPC", "logical_project_id": LOGICAL_PROJECT_ID},
    )
    assert cards.status_code == 200
    assert cards.json()["results"]
    assert all(item["logical_project_id"] == LOGICAL_PROJECT_ID for item in cards.json()["results"])

    memories = client.get(
        "/v1/memories/search",
        params={"q": "NPC", "logical_project_id": LOGICAL_PROJECT_ID},
    )
    assert memories.status_code == 200
    assert {item["project_id"] for item in memories.json()["results"]} == {
        "raw-project-a",
        "raw-project-b-nested",
    }

    agent = client.post(
        "/v1/agent/query",
        json={"query": "NPC", "logical_project_id": LOGICAL_PROJECT_ID},
    )
    assert agent.status_code == 200
    assert agent.json()["cards"]

    graph = client.get(
        "/v1/graph",
        params={"logical_project_id": LOGICAL_PROJECT_ID, "limit": 100},
    )
    assert graph.status_code == 200
    assert graph.json()["logical_project_id"] == LOGICAL_PROJECT_ID

    backlog = client.get(
        "/v1/backlog/plan",
        params={"logical_project_id": LOGICAL_PROJECT_ID, "limit_tasks": 10},
    )
    assert backlog.status_code == 200
    assert backlog.json()["planned_tasks"] == 0

    dry_run = client.post(
        "/v1/backlog/process",
        json={"logical_project_id": LOGICAL_PROJECT_ID, "limit_tasks": 10, "dry_run": True},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["dry_run"] is True


def test_backlog_failure_parks_all_task_jobs_for_retry(tmp_path, event_factory, monkeypatch):
    repo = MemoryRepository(Database(tmp_path / "backlog-retry.sqlite3"))
    _seed_task(repo, event_factory, raw_project="raw-project-a", task="task-a")
    _register_aliases(repo, ["raw-project-a"])

    class BrokenProvider:
        provider_name = "broken"
        model_name = "broken-v1"
        prompt_version = "broken-v1"

        def extract(self, context):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr("codememory.backlog.provider_from_name", lambda _: BrokenProvider())
    result = BacklogService(repo).process(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit_tasks=10,
        provider="mock",
    )
    assert result.failed == 1
    assert result.outbox_completed == 0
    assert repo.outbox_count()["pending"] == 0
    assert repo.outbox_count()["retry"] == 2
    assert result.tasks[0]["extraction"]["status"] == "failed"


def test_backlog_plan_counts_full_task_events_when_some_jobs_already_completed(
    tmp_path, event_factory
):
    repo = MemoryRepository(Database(tmp_path / "partial-backlog.sqlite3"))
    _seed_task(repo, event_factory, raw_project="raw-project-a", task="task-a")
    _register_aliases(repo, ["raw-project-a"])
    first_job = repo.claim_outbox_for_task("task-a")
    assert first_job is not None
    assert repo.complete_outbox(first_job.job_id) is True
    plan = BacklogService(repo).plan(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit_tasks=10,
        min_events=2,
    )
    assert len(plan) == 1
    assert plan[0].pending_jobs == 1
    assert plan[0].event_count == 2
    assert plan[0].coding_evidence_count == 1


def test_backlog_process_skips_non_coding_task_by_default(tmp_path, event_factory):
    repo = MemoryRepository(Database(tmp_path / "non-coding-backlog.sqlite3"))
    ingest = IngestService(repo)
    for seq, event_type, text in (
        (0, "user_message", "闲聊一下今天的计划"),
        (1, "assistant_message", "好的，今天可以继续讨论。"),
    ):
        event = event_factory(
            event_id=f"chat-task-{seq}",
            event_type=event_type,
            seq=seq,
            session_id="chat-session",
            payload={"text": text},
        )
        event["project_id"] = "raw-chat"
        event["task_id"] = "chat-task"
        event["context"] = {"root_path": "D:/P4Workspace/client/mainline"}
        ingest.ingest(event)
    _register_aliases(repo, ["raw-chat"])
    result = BacklogService(repo).process(
        logical_project_id=LOGICAL_PROJECT_ID,
        limit_tasks=10,
    )
    assert result.processed_tasks == 0
    assert result.skipped == 1
    assert result.tasks[0]["reason"] == "no_coding_evidence"
    assert repo.outbox_count()["pending"] == 2
