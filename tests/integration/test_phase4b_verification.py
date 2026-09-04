from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from codememory.api import create_app
from codememory.consolidation.store import CardStore
from codememory.quality.project_scope import logical_project_id
from codememory.quality.service import QualityService
from codememory.storage.database import Database
from codememory.storage.repository import MemoryRepository
from codememory.verification.providers import collect_p4_fstat_manifest, normalize_repo_path
from codememory.verification.service import VerificationService


LID = logical_project_id("name:project_j")
ROOT = "D:/P4Workspace/client/mainline/game_client/Project_J"
PATH = "Assets/Script/UI/Panel/MainHUDPanel/MainPanelTaskGroup/YeQuEscort/YeQuEscortTaskStageCell.cs"


def _seed_binding(repo: MemoryRepository, *, suffix: str, path: str | None = PATH, symbol: str | None = "YeQuEscortTaskStageCell") -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    project_id = f"raw-pj-{suffix}"
    task_id = f"task-pj-{suffix}"
    card_id = f"card-pj-{suffix}"
    version_id = f"version-pj-{suffix}"
    binding_id = f"binding-pj-{suffix}"
    with repo.db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects(project_id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project_id, "Project_J", now, now),
        )
        conn.execute(
            "INSERT INTO tasks(task_id,project_id,title,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (task_id, project_id, "Project_J route", "active", now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_cards(card_id,project_id,task_id,kind,canonical_key,status,confidence,current_version_id,valid_from,valid_until,metadata_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)",
            (card_id, project_id, task_id, "route_observation", f"route-{suffix}", "proposed", 0.9, version_id, now, now, now),
        )
        conn.execute(
            "INSERT INTO memory_card_versions(card_version_id,card_id,version_no,statement,aliases_json,confidence,uncertainty,source_candidate_ids_json,valid_from,valid_until,supersedes_version_id,change_reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?)",
            (version_id, card_id, 1, "Project_J route", "[\"escort\"]", 0.9, "fixture", "[]", now, "fixture"),
        )
        target = path or symbol or f"empty-{suffix}"
        conn.execute(
            "INSERT INTO memory_card_bindings(binding_id,card_version_id,role,path,symbol,qualified_symbol,normalized_target,status,snapshot_id,evidence_event_ids_json,metadata_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,NULL,'[]','{}',?,?)",
            (binding_id, version_id, "entry", path, symbol, None, target, "unverified", now, now),
        )
    QualityService(repo).resolve_project(
        project_id=project_id,
        root_path=ROOT,
        display_name="Project_J",
        persist=True,
    )
    return binding_id


def _p4_manifest(*, path: str = PATH, head: str = "2", have: str = "2") -> dict:
    return {
        "provider": "p4",
        "root_path": "D:/P4Workspace/client/mainline",
        "revision": "389032",
        "files": [{"path": f"game_client/Project_J/{path}", "head_rev": head, "have_rev": have, "head_change": "389032"}],
    }


def _cbm_manifest(*, path: str = PATH, symbol: str = "YeQuEscortTaskStageCell") -> dict:
    return {
        "provider": "codebase_memory",
        "files": [{"path": path}],
        "symbols": [{"name": symbol, "qualified_name": f"Namespace.{symbol}", "file_path": path}],
    }


def test_project_j_binding_is_verified_and_write_is_idempotent(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    binding_id = _seed_binding(repo, suffix="verified")
    service = VerificationService(repo)
    dry = service.verify(
        logical_project_id=LID,
        p4_manifest=_p4_manifest(),
        codebase_memory_manifest=_cbm_manifest(),
        write=False,
    )
    assert dry.status == "succeeded"
    assert dry.counts["by_status"] == {"verified": 1}
    with repo.db.connection() as conn:
        assert conn.execute("SELECT status FROM memory_card_bindings WHERE binding_id=?", (binding_id,)).fetchone()[0] == "unverified"
    written = service.verify(
        logical_project_id=LID,
        p4_manifest=_p4_manifest(),
        codebase_memory_manifest=_cbm_manifest(),
        write=True,
    )
    repeated = service.verify(
        logical_project_id=LID,
        p4_manifest=_p4_manifest(),
        codebase_memory_manifest=_cbm_manifest(),
        write=True,
    )
    assert written.counts["applied"] == 1
    assert repeated.counts["applied"] == 1
    # Capture timestamps are audit metadata only; replaying the same evidence
    # must retain one content-addressed snapshot and the same run input hash.
    assert written.input_hash == repeated.input_hash
    with repo.db.connection() as conn:
        assert conn.execute("SELECT status FROM memory_card_bindings WHERE binding_id=?", (binding_id,)).fetchone()[0] == "verified"
        assert conn.execute("SELECT COUNT(*) FROM binding_verifications WHERE binding_id=?", (binding_id,)).fetchone()[0] == 3
        snapshot = conn.execute(
            "SELECT manifest_hash,manifest_json FROM code_snapshots WHERE provider='composite'"
        ).fetchone()
    assert snapshot is not None
    assert snapshot["manifest_hash"]
    assert "YeQuEscortTaskStageCell.cs" in snapshot["manifest_json"]


def test_bundle_capture_metadata_is_propagated_and_hash_is_time_independent(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_binding(repo, suffix="bundle")
    service = VerificationService(repo)
    bundle = {
        "project": "Project_J",
        "captured_at": "2026-09-04T21:44:36+08:00",
        "providers": [{"provider": "codebase_memory", **_cbm_manifest()}],
    }
    first = service.verify(logical_project_id=LID, manifests=[bundle], write=False)
    second_bundle = {**bundle, "captured_at": "2026-09-05T10:00:00+08:00"}
    second = service.verify(logical_project_id=LID, manifests=[second_bundle], write=False)
    assert first.input_hash == second.input_hash
    with repo.db.connection() as conn:
        rows = conn.execute(
            "SELECT captured_at,manifest_json FROM code_snapshots WHERE provider='codebase_memory'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["captured_at"] == "2026-09-04T21:44:36+08:00"
    assert "2026-09-04T21:44:36+08:00" in rows[0]["manifest_json"]


def test_provider_arrival_order_does_not_change_input_identity(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_binding(repo, suffix="order")
    p4 = _p4_manifest()
    cbm = _cbm_manifest()
    service = VerificationService(repo)
    first = service.verify(logical_project_id=LID, manifests=[p4, cbm], write=False)
    second = service.verify(logical_project_id=LID, manifests=[cbm, p4], write=False)
    assert first.input_hash == second.input_hash
    assert first.snapshot_ids == second.snapshot_ids


def test_project_j_renamed_and_stale_states_are_explainable(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    renamed_id = _seed_binding(repo, suffix="renamed", path="Assets/Old/YeQuEscortTaskStageCell.cs")
    stale_id = _seed_binding(repo, suffix="stale")
    service = VerificationService(repo)
    renamed = service.verify(
        logical_project_id=LID,
        p4_manifest=_p4_manifest(path=PATH),
        codebase_memory_manifest=_cbm_manifest(path=PATH),
        write=True,
    )
    # The old path is absent while the symbol resolves at the new location.
    assert next(item for item in renamed.bindings if item["binding_id"] == renamed_id)["verification"]["status"] == "renamed"
    stale = service.verify(
        logical_project_id=LID,
        p4_manifest=_p4_manifest(head="9", have="8"),
        codebase_memory_manifest=_cbm_manifest(),
        write=True,
    )
    assert next(item for item in stale.bindings if item["binding_id"] == stale_id)["verification"]["status"] == "stale"


def test_missing_unavailable_and_non_project_scope_are_safe(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    binding_id = _seed_binding(repo, suffix="missing")
    service = VerificationService(repo)
    missing = service.verify(
        logical_project_id=LID,
        p4_manifest={"provider": "p4", "status": "ready", "files": [{"path": "Assets/Other.cs"}]},
        codebase_memory_manifest={"provider": "codebase_memory", "status": "ready", "files": [{"path": "Assets/Other.cs"}]},
        write=True,
    )
    assert missing.counts["by_status"] == {"missing": 1}
    unavailable = service.verify(logical_project_id=LID, write=True)
    assert unavailable.counts["by_status"] == {"unverified": 1}
    assert unavailable.provider_names == ("composite",)
    assert len(unavailable.snapshot_ids) == 1
    with repo.db.connection() as conn:
        # An unavailable provider must not demote an authoritative status.
        assert conn.execute("SELECT status FROM memory_card_bindings WHERE binding_id=?", (binding_id,)).fetchone()[0] == "missing"

    with repo.db.transaction() as conn:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        conn.execute(
            "INSERT INTO projects(project_id,name,created_at,updated_at) VALUES (?,?,?,?)",
            ("raw-other", "other", now, now),
        )
    other_project = QualityService(repo).resolve_project(
        project_id="raw-other", root_path="D:/workspace/other", display_name="other", persist=True
    )
    not_applicable = service.verify(
        logical_project_id=other_project["logical_project_id"],
        p4_manifest=_p4_manifest(),
        codebase_memory_manifest=_cbm_manifest(),
        write=True,
    )
    assert not_applicable.status == "not_applicable"


def test_project_j_scope_rejects_manifest_for_another_project(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    binding_id = _seed_binding(repo, suffix="manifest-scope")
    result = VerificationService(repo).verify(
        logical_project_id=LID,
        manifests=[
            {
                "provider": "codebase_memory",
                "project": "OtherGame",
                "status": "ready",
                "files": [{"path": PATH}],
            }
        ],
        write=True,
    )
    assert result.status == "not_applicable"
    with repo.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM code_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM memory_card_bindings WHERE binding_id=?", (binding_id,)).fetchone()[0] == "unverified"


def test_bounded_manifest_never_infers_repository_wide_missing(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_binding(repo, suffix="bounded", path="Assets/NotInSelection.cs", symbol="NotInSelection")
    result = VerificationService(repo).verify(
        logical_project_id=LID,
        manifests=[
            {
                "provider": "p4",
                "status": "ready",
                "metadata": {"coverage": "selected_paths"},
                "files": [{"path": "Assets/Selected.cs"}],
            }
        ],
        write=False,
    )
    assert result.counts["by_status"] == {"unverified": 1}
    assert "bounded snapshot" in result.bindings[0]["verification"]["reasons"][0]


def test_file_only_provider_does_not_claim_symbol_stale(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_binding(repo, suffix="p4-only")
    result = VerificationService(repo).verify(
        logical_project_id=LID,
        manifests=[
            {
                "provider": "p4",
                "status": "ready",
                "metadata": {"coverage": "selected_paths"},
                "files": [{"path": PATH}],
            }
        ],
        write=False,
    )
    assert result.counts["by_status"] == {"unverified": 1}
    assert "no authoritative symbol index" in result.bindings[0]["verification"]["reasons"][0]


def test_p4_collector_is_read_only_and_parses_ztag_output():
    class Completed:
        returncode = 0
        stdout = "\n".join(
            [
                "... depotFile //trunk/mainline/game_client/Project_J/Assets/A.cs",
                "... clientFile D:/P4Workspace/client/mainline/game_client/Project_J/Assets/A.cs",
                "... headRev 2",
                "... haveRev 2",
                "... headChange 389032",
                "... headTime 1788253470",
            ]
        )
        stderr = ""

    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    manifest = collect_p4_fstat_manifest(
        ["game_client/Project_J/Assets/A.cs"],
        root_path="D:/P4Workspace/client/mainline",
        port="p4.example:1666",
        user="tester",
        client="tester_client",
        runner=runner,
    )
    assert manifest.status == "ready"
    assert manifest.files[0].head_rev == "2"
    assert normalize_repo_path(manifest.files[0].path, roots=[manifest.root_path]) == "Assets/A.cs"
    assert calls and "fstat" in calls[0][0]
    assert calls[0][0].index("fstat") < calls[0][0].index("-Ol")

    empty = collect_p4_fstat_manifest([], runner=runner)
    assert empty.status == "unavailable"
    assert len(calls) == 1  # an empty path set must not invoke p4


def test_verification_api_exposes_report_and_write(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    repo = MemoryRepository(Database(db_path))
    _seed_binding(repo, suffix="api")
    app = create_app(db_path)
    client = TestClient(app)
    response = client.post(
        "/v1/verification/bindings",
        json={
            "logical_project_id": LID,
            "p4_manifest": _p4_manifest(),
            "codebase_memory_manifest": _cbm_manifest(),
            "write": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    report = client.get("/v1/verification/report", params={"logical_project_id": LID})
    assert report.status_code == 200
    assert report.json()["applicable"] is True
    assert report.json()["bindings"]["statuses"]["verified"] == 1
    assert "manifest" not in report.json()["snapshots"][0]
    detailed_report = client.get(
        "/v1/verification/report",
        params={"logical_project_id": LID, "include_manifest": "true"},
    )
    assert "manifest" in detailed_report.json()["snapshots"][0]
    bindings = client.get("/v1/verification/bindings", params={"logical_project_id": LID})
    assert bindings.status_code == 200
    assert bindings.json()["bindings"][0]["status"] == "verified"
    snapshot_id = response.json()["snapshot_ids"][-1]
    snapshot = client.get(
        f"/v1/verification/snapshots/{snapshot_id}",
        params={"logical_project_id": LID},
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["snapshot"]["manifest"]["provider"] == "composite"


def test_graph_merges_binding_status_into_existing_target_nodes(tmp_path):
    repo = MemoryRepository(Database(tmp_path / "memory.sqlite3"))
    _seed_binding(repo, suffix="graph")
    service = VerificationService(repo)
    service.verify(
        logical_project_id=LID,
        p4_manifest=_p4_manifest(),
        codebase_memory_manifest=_cbm_manifest(),
        task_ids=["task-pj-graph"],
        write=True,
    )
    graph = {
        "nodes": [
            {
                "id": f"file:{PATH}",
                "kind": "file",
                "label": PATH,
            }
        ],
        "edges": [],
    }
    extended = CardStore(repo.db).extend_graph(graph, task_id="task-pj-graph", limit=100)
    target = next(node for node in extended["nodes"] if node["id"] == f"file:{PATH}")
    assert target["binding_status"] == "verified"
