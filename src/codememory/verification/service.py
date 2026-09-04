"""Project_J-scoped code-binding verification service.

The service is intentionally manifest based.  P4, CodeBaseMemory, a local
filesystem scanner, or a future AST/LSP adapter can all publish the same
small snapshot contract; no provider SDK is imported by the SQLite core.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..quality.service import QualityService
from ..quality.project_scope import logical_project_id as make_logical_project_id
from ..storage.repository import MemoryRepository
from .models import (
    BindingVerification,
    SnapshotManifest,
    VERIFICATION_VERSION,
    VerificationRunResult,
)
from .providers import (
    ManifestProvider,
    collect_p4_fstat_manifest,
    normalize_repo_path,
    normalize_symbol,
    path_key,
    provider_from_manifest,
)
from .store import VerificationStore


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class VerificationService:
    """Verify current card bindings against an explicit Project_J snapshot."""

    project_j_logical_id = make_logical_project_id("name:project_j")

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        quality_service: QualityService | None = None,
        store: VerificationStore | None = None,
    ) -> None:
        self.repository = repository
        self.quality_service = quality_service or QualityService(repository)
        self.store = store or VerificationStore(repository.db)

    def _logical_project(self, logical_id: str | None) -> dict[str, Any] | None:
        with self.repository.db.connection() as conn:
            if logical_id:
                row = conn.execute(
                    "SELECT * FROM logical_projects WHERE logical_project_id=?", (logical_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM logical_projects WHERE identity_key IN ('name:project_j','name:projectj') "
                    "OR lower(replace(display_name,' ','')) IN ('project_j','projectj') "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
        if row is None:
            return None
        return {
            "logical_project_id": str(row["logical_project_id"]),
            "display_name": str(row["display_name"]),
            "identity_key": str(row["identity_key"]),
            "status": str(row["status"]),
            "metadata": _json_value(row["metadata_json"], {}),
        }

    @classmethod
    def _is_project_j(cls, project: Mapping[str, Any]) -> bool:
        logical_id = str(project.get("logical_project_id") or "")
        identity = str(project.get("identity_key") or "").casefold()
        display = str(project.get("display_name") or "").replace(" ", "").casefold()
        return (
            logical_id == cls.project_j_logical_id
            or identity in {"name:project_j", "name:projectj"}
            or display in {"project_j", "projectj"}
        )

    @staticmethod
    def _manifest_is_project_j(manifest: SnapshotManifest) -> bool:
        """Accept explicit Project_J labels and its known index-path form."""

        raw = str(manifest.project or "").strip().casefold().replace("\\", "/")
        if raw in {"project_j", "projectj"}:
            return True
        # CodeBaseMemory's local view name is commonly a long path ending in
        # ``Project_J``.  Require a token boundary so an unrelated project
        # named ``project_j2`` is not accepted accidentally.
        return bool(re.search(r"(?:^|[/\\:_-])project[_-]?j(?:$|[/\\:_-])", raw))

    @staticmethod
    def _flatten_manifests(value: Any) -> list[tuple[str | None, Mapping[str, Any] | SnapshotManifest]]:
        if value is None:
            return []
        if isinstance(value, SnapshotManifest):
            return [(value.provider, value)]
        if isinstance(value, Mapping):
            providers = value.get("providers") or value.get("manifests")
            # A bundle commonly carries one capture timestamp/project/root
            # shared by all provider payloads.  Propagate those fields before
            # normalizing children; otherwise each child would receive a new
            # local ``utc_now()`` and identical bundles would hash differently.
            shared = {
                key: value[key]
                for key in ("project", "project_name", "root_path", "root", "workspace", "captured_at", "capturedAt")
                if value.get(key) not in (None, "")
            }
            if isinstance(providers, Mapping):
                result: list[tuple[str | None, Mapping[str, Any] | SnapshotManifest]] = []
                for name, item in providers.items():
                    if isinstance(item, Mapping):
                        child = {**shared, **item, "provider": name}
                        result.extend(VerificationService._flatten_manifests(child))
                    elif isinstance(item, SnapshotManifest):
                        child = item.canonical_dict()
                        child.update({key: value for key, value in shared.items() if key not in child})
                        child["provider"] = name
                        result.extend(VerificationService._flatten_manifests(child))
                return result
            if isinstance(providers, list):
                result = []
                for item in providers:
                    if isinstance(item, Mapping):
                        result.extend(VerificationService._flatten_manifests({**shared, **item}))
                    elif isinstance(item, SnapshotManifest):
                        child = item.canonical_dict()
                        child.update({key: value for key, value in shared.items() if key not in child})
                        result.extend(VerificationService._flatten_manifests(child))
                return result
            return [(str(value.get("provider")) if value.get("provider") else None, value)]
        if isinstance(value, (list, tuple)):
            result = []
            for item in value:
                result.extend(VerificationService._flatten_manifests(item))
            return result
        raise ValueError("verification manifests must be objects or a list of objects")

    @classmethod
    def _providers(
        cls,
        manifests: Iterable[Any],
        *,
        p4_manifest: Any = None,
        codebase_memory_manifest: Any = None,
    ) -> tuple[list[SnapshotManifest], list[ManifestProvider]]:
        rows: list[tuple[str | None, Mapping[str, Any] | SnapshotManifest]] = []
        for item in manifests:
            rows.extend(cls._flatten_manifests(item))
        if p4_manifest is not None:
            rows.extend(cls._flatten_manifests({**p4_manifest, "provider": "p4"} if isinstance(p4_manifest, Mapping) else p4_manifest))
        if codebase_memory_manifest is not None:
            rows.extend(
                cls._flatten_manifests(
                    {**codebase_memory_manifest, "provider": "codebase_memory"}
                    if isinstance(codebase_memory_manifest, Mapping)
                    else codebase_memory_manifest
                )
            )
        normalized: list[SnapshotManifest] = []
        seen: set[tuple[str, str]] = set()
        for provider_hint, raw in rows:
            manifest = SnapshotManifest.from_mapping(raw, provider_override=provider_hint)
            key = (manifest.provider, manifest.manifest_hash)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(manifest)
        normalized.sort(key=lambda item: (item.provider, item.manifest_hash))
        providers = [provider_from_manifest(item) for item in normalized]
        return normalized, providers

    @staticmethod
    def _provider_result(provider: ManifestProvider, binding: Mapping[str, Any]) -> dict[str, Any]:
        return provider.resolve(binding)

    @staticmethod
    def _entry_path(result: Mapping[str, Any], section: str) -> str:
        entry = (result.get(section) or {}).get("entry")
        if not isinstance(entry, Mapping):
            return ""
        return normalize_repo_path(entry.get("path") or entry.get("file_path") or entry.get("filePath"))

    @staticmethod
    def _p4_stale(result: Mapping[str, Any]) -> bool:
        entry = (result.get("path") or {}).get("entry")
        if not isinstance(entry, Mapping):
            return False
        head = str(entry.get("head_rev") or "").strip()
        have = str(entry.get("have_rev") or "").strip()
        return bool(head and have and head != have)

    @classmethod
    def _evaluate(
        cls,
        binding: Mapping[str, Any],
        providers: Sequence[ManifestProvider],
        composite: ManifestProvider,
        *,
        absence_authoritative: bool = True,
    ) -> BindingVerification:
        binding_id = str(binding.get("binding_id") or "")
        raw_path = str(binding.get("path") or "").strip()
        raw_symbol = str(binding.get("symbol") or "").strip()
        raw_qualified = str(binding.get("qualified_symbol") or "").strip()
        has_path = bool(raw_path)
        has_symbol = bool(raw_symbol or raw_qualified)
        requested_path = normalize_repo_path(raw_path)
        requested_symbol = normalize_symbol(raw_qualified or raw_symbol)
        reasons: list[str] = []

        # Reject obvious non-bindings before asking an external provider.  This
        # mirrors the Phase 4A quality gate and prevents URLs/globs from being
        # treated as source files when a provider happens to echo them.
        lowered_path = raw_path.casefold()
        if (not has_path and not has_symbol) or "*" in raw_path or "?" in raw_path or lowered_path.startswith(("http://", "https://", "skill:")):
            reasons.append("binding has no concrete path/symbol or contains a non-source placeholder")
            return BindingVerification(
                binding_id=binding_id,
                status="rejected",
                score=0.0,
                evidence={"requested_path": raw_path, "requested_symbol": requested_symbol},
                reasons=tuple(reasons),
            )

        provider_results = [cls._provider_result(provider, binding) for provider in providers]
        composite_result = cls._provider_result(composite, binding)
        available = [
            result
            for result in provider_results
            if str(result.get("snapshot_status") or "") in {"ready", "partial"}
        ]
        authoritative = bool(available)
        path_result = composite_result.get("path") or {}
        symbol_result = composite_result.get("symbol") or {}
        path_found = bool(path_result.get("found"))
        symbol_found = bool(symbol_result.get("found"))
        resolved_path = ""
        resolved_symbol = ""
        if isinstance(symbol_result.get("entry"), Mapping):
            resolved_symbol = str(
                symbol_result["entry"].get("qualified_name")
                or symbol_result["entry"].get("name")
                or ""
            )
            resolved_path = normalize_repo_path(
                symbol_result["entry"].get("file_path") or ""
            )
        if not resolved_path and isinstance(path_result.get("entry"), Mapping):
            resolved_path = normalize_repo_path(path_result["entry"].get("path") or "")
        if not resolved_symbol and isinstance(symbol_result.get("entry"), Mapping):
            resolved_symbol = str(symbol_result["entry"].get("name") or "")

        p4_results = [
            result for result in provider_results if result.get("provider") == "p4"
        ]
        cbm_results = [
            result for result in provider_results if result.get("provider") == "codebase_memory"
        ]
        p4_path_found = any(bool((item.get("path") or {}).get("found")) for item in p4_results)
        cbm_path_found = any(bool((item.get("path") or {}).get("found")) for item in cbm_results)
        p4_symbol_found = any(bool((item.get("symbol") or {}).get("found")) for item in p4_results)
        cbm_symbol_found = any(bool((item.get("symbol") or {}).get("found")) for item in cbm_results)
        p4_stale = any(cls._p4_stale(item) for item in p4_results)
        # P4 fstat is a file/revision index, not a symbol index.  A path being
        # present in a P4-only (or file-only) manifest must not be interpreted
        # as proof that an omitted class/method is stale.  Non-empty symbol
        # evidence, or an adapter's explicit symbol_coverage declaration, is
        # required before making that inference.
        symbol_index_available = any(
            int(item.get("symbol_count") or 0) > 0
            or str(item.get("symbol_coverage") or "").casefold()
            in {"complete", "full", "repository", "selected_file", "selected_files"}
            for item in provider_results
        )
        resolved_symbol_path = resolved_path
        requested_key = path_key(requested_path)
        resolved_key = path_key(resolved_symbol_path)
        path_mismatch = bool(has_path and resolved_key and requested_key and resolved_key != requested_key)

        evidence: dict[str, Any] = {
            "verification_version": VERIFICATION_VERSION,
            "requested_path": raw_path,
            "normalized_path": requested_path,
            "requested_symbol": requested_symbol,
            "providers": [
                {
                    "provider": str(item.get("provider") or ""),
                    "snapshot_status": str(item.get("snapshot_status") or ""),
                    "path_found": bool((item.get("path") or {}).get("found")),
                    "symbol_found": bool((item.get("symbol") or {}).get("found")),
                    "symbol_match_type": (item.get("symbol") or {}).get("match_type"),
                }
                for item in provider_results
            ],
            "p4_path_found": p4_path_found,
            "p4_symbol_found": p4_symbol_found,
            "codebase_memory_path_found": cbm_path_found,
            "codebase_memory_symbol_found": cbm_symbol_found,
            "p4_workspace_stale": p4_stale,
            "symbol_index_available": symbol_index_available,
            "absence_authoritative": absence_authoritative,
        }
        if not authoritative:
            reasons.append("no ready or partial provider snapshot was available")
            return BindingVerification(
                binding_id=binding_id,
                status="unverified",
                score=0.2,
                resolved_path=resolved_path or None,
                resolved_symbol=resolved_symbol or None,
                evidence=evidence,
                reasons=tuple(reasons),
            )

        if has_path and has_symbol:
            if symbol_found:
                if path_mismatch:
                    reasons.append("symbol resolves to a different current file")
                    status, score = "renamed", 0.72
                elif p4_stale:
                    reasons.append("P4 haveRev differs from headRev for the bound file")
                    status, score = "stale", 0.62
                else:
                    reasons.append("requested path and symbol resolve in the snapshot")
                    status, score = "verified", 1.0 if p4_path_found or cbm_path_found else 0.86
            elif path_found and (symbol_index_available or p4_stale):
                reasons.append("file exists but the requested symbol is absent from the index")
                status, score = "stale", 0.58
            elif path_found:
                reasons.append("file exists, but the supplied snapshot has no authoritative symbol index")
                status, score = "unverified", 0.2
            elif authoritative and absence_authoritative:
                reasons.append("neither requested path nor symbol exists in the snapshot")
                status, score = "missing", 0.0
            elif authoritative:
                reasons.append("bounded snapshot does not contain the requested path or symbol")
                status, score = "unverified", 0.2
            else:  # pragma: no cover - authoritative is true above
                status, score = "unverified", 0.2
        elif has_path:
            if path_found:
                if p4_stale:
                    reasons.append("P4 workspace is behind the head revision")
                    status, score = "stale", 0.62
                else:
                    reasons.append("requested file exists in the snapshot")
                    status, score = "verified", 1.0 if p4_path_found else 0.86
            elif authoritative and absence_authoritative:
                reasons.append("requested file is absent from the snapshot")
                status, score = "missing", 0.0
            elif authoritative:
                reasons.append("bounded snapshot does not cover the requested file")
                status, score = "unverified", 0.2
            else:  # pragma: no cover
                status, score = "unverified", 0.2
        else:  # symbol only
            if symbol_found:
                if p4_stale:
                    reasons.append("symbol exists but its P4 workspace revision is stale")
                    status, score = "stale", 0.62
                else:
                    reasons.append("requested symbol resolves in the snapshot")
                    status, score = "verified", 1.0 if cbm_symbol_found else 0.82
            elif authoritative and absence_authoritative and symbol_index_available:
                reasons.append("requested symbol is absent from the snapshot")
                status, score = "missing", 0.0
            elif authoritative:
                reasons.append("bounded snapshot does not cover the requested symbol")
                status, score = "unverified", 0.2
            else:  # pragma: no cover
                status, score = "unverified", 0.2

        if any(str(item.get("snapshot_status")) == "partial" for item in provider_results):
            reasons.append("one or more provider manifests are partial; result remains auditable")
        evidence["resolved_path"] = resolved_path or None
        evidence["resolved_symbol"] = resolved_symbol or None
        evidence["path_mismatch"] = path_mismatch
        return BindingVerification(
            binding_id=binding_id,
            status=status,
            score=score,
            resolved_path=resolved_path or None,
            resolved_symbol=resolved_symbol or None,
            evidence=evidence,
            reasons=tuple(reasons),
        )

    def verify(
        self,
        *,
        logical_project_id: str | None = None,
        manifests: Iterable[Any] = (),
        p4_manifest: Any = None,
        codebase_memory_manifest: Any = None,
        p4_paths: Sequence[str] | None = None,
        p4_options: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        task_ids: Sequence[str] = (),
        write: bool = False,
        limit: int = 100_000,
        include_quarantine: bool = False,
    ) -> VerificationRunResult:
        project = self._logical_project(logical_project_id)
        resolved_id = str(project.get("logical_project_id")) if project else str(logical_project_id or self.project_j_logical_id)
        if project is None or not self._is_project_j(project):
            return VerificationRunResult(
                run_id=None,
                logical_project_id=resolved_id,
                mode="write" if write else "dry_run",
                status="not_applicable",
                input_hash=None,
                counts={"bindings": 0, "not_applicable": 1},
                reason="P4/CodeBaseMemory verification is intentionally enabled only for the Project_J logical scope",
            )

        # P4 is collected only after the logical-scope guard.  This is an
        # important isolation property: passing P4 options can never cause a
        # network/VCS call for another project.
        manifest_inputs = list(manifests)
        if p4_paths:
            options = dict(p4_options or {})
            p4_manifest = collect_p4_fstat_manifest(p4_paths, **options)
        normalized, providers = self._providers(
            manifest_inputs,
            p4_manifest=p4_manifest,
            codebase_memory_manifest=codebase_memory_manifest,
        )
        mismatched = [item.provider for item in normalized if not self._manifest_is_project_j(item)]
        if mismatched:
            return VerificationRunResult(
                run_id=None,
                logical_project_id=resolved_id,
                mode="write" if write else "dry_run",
                status="not_applicable",
                input_hash=None,
                counts={"bindings": 0, "not_applicable": 1},
                reason=(
                    "provider manifest project does not match Project_J: "
                    + ", ".join(sorted(set(mismatched)))
                ),
            )
        if not normalized:
            # Persist one explicit unavailable composite snapshot rather than
            # recursively merging an already-composite placeholder (which
            # would create two semantically identical audit snapshots).
            normalized = []
            providers = []
            composite_manifest = SnapshotManifest(
                provider="composite",
                project="Project_J",
                status="unavailable",
            )
        else:
            base_manifests = [item for item in normalized if item.provider != "composite"]
            composite_manifest = SnapshotManifest.merge(
                base_manifests or normalized,
                project="Project_J",
            )
        composite = provider_from_manifest(composite_manifest)
        absence_authoritative = all(
            item.status == "ready"
            and item.coverage in {"complete", "full", "repository"}
            for item in normalized
            if item.status in {"ready", "partial"}
        )
        all_manifests = normalized + (
            [composite_manifest]
            if composite_manifest.manifest_hash not in {item.manifest_hash for item in normalized}
            else []
        )
        snapshots = [self.store.ensure_snapshot(resolved_id, item) for item in all_manifests]
        composite_snapshot = next(
            item for item in snapshots if item["manifest_hash"] == composite_manifest.manifest_hash
        )
        raw_ids = self.quality_service.store.raw_project_ids_for_logical(resolved_id)
        bindings = self.store.list_current_bindings(
            raw_ids,
            task_id=task_id,
            task_ids=task_ids,
            limit=limit,
            include_quarantine=include_quarantine,
        )
        canonical = {
            "verification_version": VERIFICATION_VERSION,
            "logical_project_id": resolved_id,
            "binding_ids": [str(item["binding_id"]) for item in bindings],
            "task_id": task_id,
            "task_ids": sorted({str(item) for item in task_ids if str(item)}),
            # Capture timestamps are audit metadata, not input identity.  Use
            # deterministic content projections so replaying the same
            # provider evidence is idempotent across process invocations.
            "manifests": [item.content_dict() for item in all_manifests],
            "mode": "write" if write else "dry_run",
            "include_quarantine": bool(include_quarantine),
        }
        input_hash = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        run_id = self.store.start_run(
            logical_project_id=resolved_id,
            input_hash=input_hash,
            mode="write" if write else "dry_run",
            provider_names=[item.provider for item in all_manifests],
            snapshot_ids=[item["snapshot_id"] for item in snapshots],
        )
        try:
            checks = [
                self._evaluate(
                    item,
                    providers,
                    composite,
                    absence_authoritative=absence_authoritative,
                )
                for item in bindings
            ]
            results = [item.as_dict() for item in checks]
            persisted = self.store.record_results(
                run_id=run_id,
                snapshot_id_value=str(composite_snapshot["snapshot_id"]),
                bindings=bindings,
                results=results,
                apply_status=write,
            )
            counts = {
                "bindings": len(bindings),
                "by_status": dict(Counter(item.status for item in checks)),
                **persisted,
                "providers": {
                    item.provider: {
                        "status": item.status,
                        "files": item.file_count,
                        "symbols": item.symbol_count,
                    }
                    for item in all_manifests
                },
            }
            self.store.finish_run(run_id, status="succeeded", counts=counts)
            enriched: list[dict[str, Any]] = []
            for binding, check in zip(bindings, checks):
                value = dict(binding)
                verification = check.as_dict()
                verification["applied"] = bool(
                    write
                    and (
                        check.status != "unverified"
                        or str(binding.get("status") or "unverified") == "unverified"
                    )
                )
                value["verification"] = verification
                value["snapshot_id"] = str(composite_snapshot["snapshot_id"])
                enriched.append(value)
            return VerificationRunResult(
                run_id=run_id,
                logical_project_id=resolved_id,
                mode="write" if write else "dry_run",
                status="succeeded",
                input_hash=input_hash,
                provider_names=tuple(item.provider for item in all_manifests),
                snapshot_ids=tuple(item["snapshot_id"] for item in snapshots),
                counts=counts,
                bindings=tuple(enriched),
            )
        except Exception as exc:
            self.store.finish_run(run_id, status="failed", counts={}, error=f"{type(exc).__name__}: {exc}")
            raise

    def report(
        self,
        *,
        logical_project_id: str | None = None,
        limit: int = 20,
        include_manifest: bool = False,
    ) -> dict[str, Any]:
        project = self._logical_project(logical_project_id)
        resolved_id = str(project.get("logical_project_id")) if project else str(logical_project_id or self.project_j_logical_id)
        if project is None or not self._is_project_j(project):
            return {
                "logical_project_id": resolved_id,
                "applicable": False,
                "reason": "Project_J is the only scope with live P4/CodeBaseMemory verification",
                "bindings": {"total": 0, "statuses": {}},
                "checks": {"total": 0, "statuses": {}},
                "snapshots": [],
                "latest_runs": [],
            }
        raw_ids = self.quality_service.store.raw_project_ids_for_logical(resolved_id)
        result = self.store.report(
            logical_project_id=resolved_id,
            raw_project_ids=raw_ids,
            limit=limit,
            include_manifest=include_manifest,
        )
        result.update(
            {
                "applicable": True,
                "display_name": project.get("display_name"),
                "identity_key": project.get("identity_key"),
                "verification_version": VERIFICATION_VERSION,
            }
        )
        return result

    def snapshot(
        self,
        snapshot_id_value: str,
        *,
        logical_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one audited snapshot, including its exact manifest content."""

        project = self._logical_project(logical_project_id)
        resolved_id = (
            str(project.get("logical_project_id"))
            if project
            else str(logical_project_id or self.project_j_logical_id)
        )
        if project is None or not self._is_project_j(project):
            return {
                "logical_project_id": resolved_id,
                "applicable": False,
                "snapshot": None,
                "reason": "Project_J is the only scope with live P4/CodeBaseMemory verification",
            }
        value = self.store.get_snapshot(snapshot_id_value, include_manifest=True)
        if value is None or str(value.get("logical_project_id")) != resolved_id:
            return {
                "logical_project_id": resolved_id,
                "applicable": True,
                "snapshot": None,
                "reason": "snapshot_not_found",
            }
        return {
            "logical_project_id": resolved_id,
            "applicable": True,
            "snapshot": value,
            "verification_version": VERIFICATION_VERSION,
        }

    def list_bindings(
        self,
        *,
        logical_project_id: str | None = None,
        status: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        project = self._logical_project(logical_project_id)
        resolved_id = str(project.get("logical_project_id")) if project else str(logical_project_id or self.project_j_logical_id)
        if project is None or not self._is_project_j(project):
            return {"logical_project_id": resolved_id, "applicable": False, "bindings": []}
        raw_ids = self.quality_service.store.raw_project_ids_for_logical(resolved_id)
        return {
            "logical_project_id": resolved_id,
            "applicable": True,
            "bindings": self.store.list_verifications(
                logical_project_id=resolved_id,
                raw_project_ids=raw_ids,
                status=status,
                limit=limit,
            ),
        }


def _json_value(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return default
