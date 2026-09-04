from __future__ import annotations

from pathlib import Path

from codememory.extraction.service import ExtractionService
from codememory.extraction.store import ExtractionStore
from codememory.history.codex import CodexHistoryImporter
from codememory.ingest.service import IngestService
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository


def test_selected_codex_histories_are_real_event_input(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    service = IngestService(repo)
    fixture = Path(__file__).parents[2] / "fixtures" / "codex-history" / "selected-threads.json"
    importer = CodexHistoryImporter(service)
    first = importer.import_file(fixture)
    second = importer.import_file(fixture)
    assert first.threads == 3
    assert first.events >= 30
    assert first.accepted == first.events
    assert second.duplicates == first.events
    health = repo.health()
    assert health["counts"]["events"] == first.events
    assert health["counts"]["tasks"] == 3
    with repo.db.connection() as conn:
        row = conn.execute(
            "SELECT payload_json,context_json,source,completeness FROM events "
            "WHERE task_id=? AND event_type='user_message' ORDER BY seq LIMIT 1",
            (first.tasks[0],),
        ).fetchone()
    assert row["source"] == "replay"
    assert row["completeness"] == "partial"
    assert "thread_id" in row["payload_json"]
    assert "codex_thread_id" in row["context_json"]


def test_each_imported_task_can_produce_candidate_memory(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    fixture = Path(__file__).parents[2] / "fixtures" / "codex-history" / "selected-threads.json"
    summary = CodexHistoryImporter(IngestService(repo)).import_file(fixture)
    service = ExtractionService(repo, store=ExtractionStore(repo.db))
    results = [service.extract_task(task_id) for task_id in summary.tasks]
    assert all(result.status == "extracted" for result in results)
    assert sum(result.candidate_count for result in results) >= 3
    graph = ExtractionStore(repo.db).graph(task_id=summary.tasks[1])
    assert any(node["kind"] == "memory" for node in graph["nodes"])
    assert any(edge["relation"] == "supports" for edge in graph["edges"])


def test_history_without_timestamps_remains_idempotent(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    importer = CodexHistoryImporter(IngestService(repo))
    payload = {
        "thread": {"id": "thread-no-time", "title": "No timestamp", "cwd": "D:/repo"},
        "turns": [{"id": "turn-1", "role": "user", "text": "Find ExampleService"}],
    }
    events, _, _ = importer.thread_events(payload)
    first = importer.service.ingest_many(events, atomic=True)
    events_again, _, _ = importer.thread_events(payload)
    second = importer.service.ingest_many(events_again, atomic=True)
    assert all(result.status == "accepted" for result in first)
    assert all(result.status == "duplicate" for result in second)
    assert [event.occurred_at for event in events] == [event.occurred_at for event in events_again]


def test_read_thread_newest_first_page_is_normalized(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    importer = CodexHistoryImporter(IngestService(repo))
    payload = {
        "thread": {"id": "thread-newest-first", "title": "Newest first", "cwd": "D:/repo"},
        "page": {"order": "newest_first"},
        "turns": [
            {"id": "turn-2", "startedAt": 2, "role": "user", "text": "second"},
            {"id": "turn-1", "startedAt": 1, "role": "user", "text": "first"},
        ],
    }
    events, _, _ = importer.thread_events(payload)
    user_events = [event for event in events if event.event_type.value == "user_message"]
    assert [event.payload["text"] for event in user_events] == ["first", "second"]
    assert [event.seq for event in user_events] == [1, 2]


def test_host_injected_context_is_not_persisted_as_coding_evidence(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    importer = CodexHistoryImporter(IngestService(repo))
    payload = {
        "thread": {"id": "thread-host-context", "title": "Context filter", "cwd": "D:/repo"},
        "turns": [
            {
                "idx": 0,
                "role": "user",
                "text": "修复 ExampleService\n<recommended_plugins>secret host catalog</recommended_plugins>",
            },
            {
                "idx": 1,
                "role": "assistant",
                "text": "<analysis>private reasoning</analysis>已完成 ExampleService.cs",
            },
        ],
    }
    events, task_id, _ = importer.thread_events(payload)
    importer.service.ingest_many(events)
    timeline = repo.timeline(task_id)
    texts = [str(event["payload"].get("text") or "") for event in timeline]
    assert all("secret host catalog" not in text for text in texts)
    assert all("private reasoning" not in text for text in texts)
    assert any("ExampleService" in text for text in texts)


def test_title_and_nested_visible_values_are_sanitized(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    importer = CodexHistoryImporter(IngestService(repo))
    payload = {
        "thread": {
            "id": "thread-title-context",
            "title": "修复 ExampleService <environment_context>host-only</environment_context>",
            "cwd": "D:/repo",
        },
        "turns": [
            {
                "id": "tool-1",
                "items": [
                    {
                        "id": "call-1",
                        "type": "mcpToolCall",
                        "tool": "read_file",
                        "arguments": {
                            "path": "ExampleService.cs",
                            "note": "<analysis>hidden</analysis>visible",
                        },
                    }
                ],
            }
        ],
    }
    events, task_id, _ = importer.thread_events(payload)
    importer.service.ingest_many(events)
    timeline = repo.timeline(task_id)
    encoded = " ".join(str(event["payload"]) + str(event["context"]) for event in timeline)
    assert "host-only" not in encoded
    assert "hidden" not in encoded
    assert "ExampleService.cs" in encoded


def test_mock_provider_does_not_create_memory_for_empty_admin_thread(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    importer = CodexHistoryImporter(IngestService(repo))
    payload = {
        "thread": {"id": "thread-empty", "title": "019f0000000000000000000000000000", "cwd": "D:/repo"},
        "turns": [],
    }
    events, task_id, _ = importer.thread_events(payload)
    importer.service.ingest_many(events)
    result = ExtractionService(repo, store=ExtractionStore(repo.db)).extract_task(task_id)
    assert result.status == "extracted"
    assert result.candidate_count == 0


def test_synthetic_instruction_rows_are_dropped_but_attachment_request_survives(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    importer = CodexHistoryImporter(IngestService(repo))
    payload = {
        "thread": {
            "id": "thread-instruction-rows",
            "title": "真实任务标题",
            "cwd": "D:/repo",
            "file_paths": ["D:/Tool/AI_Project_J/AGENTS.md", "D:/Tool/AI_Project_J/skills/foo/SKILL.md"],
        },
        "turns": [
            {
                "idx": 0,
                "role": "user",
                "text": "# AGENTS.md instructions for D:/repo\n<INSTRUCTIONS>platform-only</INSTRUCTIONS>",
            },
            {
                "idx": 1,
                "role": "user",
                "text": "# Files mentioned by the user:\n\n## attachment.txt\n\n## My request:\n修复 ExampleService.cs 的初始化逻辑",
            },
            {
                "idx": 2,
                "role": "user",
                "text": "<skill>\n<name>project-j-code-discovery</name>\n完整 skill 文档，不是任务事实",
            },
        ],
    }
    events, task_id, _ = importer.thread_events(payload)
    importer.service.ingest_many(events)
    texts = [str(event["payload"].get("text") or "") for event in repo.timeline(task_id)]
    assert any("修复 ExampleService.cs" in text for text in texts)
    assert all("platform-only" not in text for text in texts)
    assert all("完整 skill 文档" not in text for text in texts)
    assert all("AGENTS.md instructions" not in text for text in texts)


def test_session_file_hints_do_not_become_code_bindings(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "history.sqlite3"))
    importer = CodexHistoryImporter(IngestService(repo))
    payload = {
        "thread": {
            "id": "thread-file-hints",
            "title": "修复 ExampleService 初始化",
            "cwd": "D:/repo",
            "file_paths": ["D:/Tool/AI_Project_J/AGENTS.md", "D:/Tool/AI_Project_J/skills/foo/SKILL.md"],
        },
        "turns": [
            {"idx": 0, "role": "user", "text": "修复 ExampleService.cs 初始化逻辑"},
        ],
    }
    events, task_id, _ = importer.thread_events(payload)
    importer.service.ingest_many(events)
    result = ExtractionService(repo, store=ExtractionStore(repo.db)).extract_task(task_id)
    assert result.candidate_count >= 1
    candidates = ExtractionStore(repo.db).list_candidates(task_id=task_id)
    bindings = [binding for candidate in candidates for binding in candidate["bindings"]]
    assert all("AGENTS.md" not in str(binding.get("path")) for binding in bindings)
    assert all("SKILL.md" not in str(binding.get("path")) for binding in bindings)
