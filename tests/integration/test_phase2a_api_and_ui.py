from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codememory.api import create_app
from codememory.history.codex import CodexHistoryImporter


def test_graph_memory_routes_and_static_ui(tmp_path):
    app = create_app(tmp_path / "memory.sqlite3")
    client = TestClient(app)
    fixture = Path(__file__).parents[2] / "fixtures" / "codex-history" / "selected-threads.json"
    summary = CodexHistoryImporter(app.state.ingest_service).import_file(fixture)
    extracted = app.state.extraction_service.extract_task(summary.tasks[0])
    assert extracted.status == "extracted"
    tasks = client.get("/v1/tasks")
    assert tasks.status_code == 200
    assert len(tasks.json()["tasks"]) == 3
    memories = client.get(f"/v1/tasks/{summary.tasks[0]}/memories")
    assert memories.status_code == 200
    assert memories.json()["memories"]
    candidate = memories.json()["memories"][0]
    detail = client.get(f"/v1/memories/{candidate['candidate_id']}")
    assert detail.status_code == 200
    assert detail.json()["candidate_id"] == candidate["candidate_id"]
    search = client.get("/v1/memories/search", params={"q": "CustomGameplayTabHost"})
    assert search.status_code == 200
    assert search.json()["results"]
    graph = client.get(f"/v1/graph?task_id={summary.tasks[0]}")
    assert graph.status_code == 200
    body = graph.json()
    assert any(node["kind"] == "memory" for node in body["nodes"])
    assert any(edge["relation"] == "supports" for edge in body["edges"])
    page = client.get("/")
    assert page.status_code == 200
    assert "CodeMemory" in page.text
    assert "three" in page.text.lower()
    assert client.get("/web/app.js").status_code == 200
