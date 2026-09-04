"""Portable models for code-binding verification snapshots.

The verification layer deliberately consumes a small manifest instead of
depending on a particular VCS, AST engine, LSP, or MCP implementation.  An
adapter can therefore turn P4/CodeBaseMemory output into this contract and the
SQLite core remains fully local and deterministic.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


VERIFICATION_SCHEMA_VERSION = "codememory.verification_manifest.v1"
VERIFICATION_VERSION = "binding-verification-v1"
VERIFICATION_STATUSES = frozenset(
    {"verified", "missing", "renamed", "stale", "unverified", "rejected"}
)
SNAPSHOT_STATUSES = frozenset({"ready", "partial", "unavailable", "failed"})
PROVIDERS = frozenset({"p4", "codebase_memory", "filesystem", "composite"})


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() not in {"", "0", "false", "no", "none", "null"}


@dataclass(frozen=True)
class SnapshotFile:
    """One repository file entry in a provider snapshot."""

    path: str
    exists: bool = True
    head_rev: str | None = None
    have_rev: str | None = None
    head_change: str | None = None
    head_time: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotSymbol:
    """One symbol (usually a class/method) indexed by a provider."""

    name: str
    qualified_name: str | None = None
    file_path: str | None = None
    kind: str | None = None
    exists: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotManifest:
    """Normalized, hashable provider snapshot manifest."""

    provider: str
    project: str = "Project_J"
    root_path: str | None = None
    revision: str | None = None
    source_ref: str | None = None
    status: str = "ready"
    files: tuple[SnapshotFile, ...] = ()
    symbols: tuple[SnapshotSymbol, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    captured_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        provider = str(self.provider).strip().casefold()
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported verification provider: {self.provider}")
        object.__setattr__(self, "provider", provider)
        status = str(self.status).strip().casefold() or "ready"
        if status not in SNAPSHOT_STATUSES:
            raise ValueError(f"unsupported snapshot status: {self.status}")
        object.__setattr__(self, "status", status)
        if not self.files and not self.symbols and status == "ready":
            # A syntactically valid but empty provider result is not an
            # authoritative snapshot; classify it as partial instead of
            # turning every binding into ``missing``.
            object.__setattr__(self, "status", "partial")

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def symbol_count(self) -> int:
        return len(self.symbols)

    @property
    def coverage(self) -> str:
        """Coverage declaration used to make absence decisions safe.

        ``complete`` means the provider represents the whole repository;
        ``selected``/``requested_paths`` means it is a bounded lookup and a
        missing item must remain ``unverified`` rather than ``missing``.
        """

        return str(self.metadata.get("coverage") or "complete").casefold()

    def canonical_dict(self) -> dict[str, Any]:
        """Return the complete persisted representation of this snapshot.

        ``captured_at`` is intentionally retained here for audit/display
        purposes.  It is not part of the content identity; see
        :meth:`content_dict` and :attr:`manifest_hash` below.
        """
        return {
            "schema_version": VERIFICATION_SCHEMA_VERSION,
            "provider": self.provider,
            "project": self.project,
            "root_path": self.root_path,
            "revision": self.revision,
            "source_ref": self.source_ref,
            "status": self.status,
            "files": [item.as_dict() for item in sorted(self.files, key=lambda x: x.path)],
            "symbols": [
                item.as_dict()
                for item in sorted(
                    self.symbols,
                    key=lambda x: (x.qualified_name or x.name, x.file_path or ""),
                )
            ],
            "metadata": self.metadata,
            "captured_at": self.captured_at,
        }

    def content_dict(self) -> dict[str, Any]:
        """Return the deterministic, time-independent snapshot content.

        A provider can be queried repeatedly without changing any source
        fact.  The capture timestamp must therefore not create a new logical
        snapshot or input hash on every run.  Keeping this projection separate
        from :meth:`canonical_dict` also makes the identity rule explicit for
        adapters and future schema migrations.
        """

        value = self.canonical_dict()
        value.pop("captured_at", None)
        return value

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(_json(self.content_dict()).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        value = self.canonical_dict()
        value["manifest_hash"] = self.manifest_hash
        value["file_count"] = self.file_count
        value["symbol_count"] = self.symbol_count
        return value

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | SnapshotManifest,
        *,
        provider_override: str | None = None,
    ) -> "SnapshotManifest":
        if isinstance(raw, SnapshotManifest):
            if provider_override and raw.provider != provider_override:
                return cls(
                    provider=provider_override,
                    project=raw.project,
                    root_path=raw.root_path,
                    revision=raw.revision,
                    source_ref=raw.source_ref,
                    status=raw.status,
                    files=raw.files,
                    symbols=raw.symbols,
                    metadata=raw.metadata,
                    captured_at=raw.captured_at,
                )
            return raw
        if not isinstance(raw, Mapping):
            raise ValueError("verification manifest must be an object")
        value = _unwrap_mapping(raw)
        provider = provider_override or value.get("provider") or value.get("source") or "filesystem"
        provider = str(provider).strip().casefold().replace("-", "_")
        if provider in {"codebasememory", "codebase-memory", "graph"}:
            provider = "codebase_memory"
        if provider in {"perforce", "p4_fstat"}:
            provider = "p4"

        files = _parse_files(value)
        symbols = _parse_symbols(value)
        # Graph MCP responses often expose nodes rather than explicit files /
        # symbols.  Parse those fields without importing an MCP SDK.
        for node in _iter_nodes(value):
            kind = str(node.get("kind") or node.get("type") or "").casefold()
            path = _first(node, "path", "file_path", "filePath", "clientFile", "client_file")
            label = _first(node, "label", "name", "symbol", "qualified_name", "qualifiedName")
            qualified = _first(node, "qualified_name", "qualifiedName", "qualified_symbol", "qualifiedSymbol")
            name = _first(node, "name", "symbol", "short_name", "shortName")
            if kind in {"module", "file", "source_file", "document"} and path:
                # CodeBaseMemory exposes a module node whose ``name`` is the
                # full path.  A binding normally carries the file stem, so
                # derive that stable short symbol while retaining the module
                # qualified name as evidence.
                stem = posixpath.basename(str(path)).rsplit(".", 1)[0]
                if stem:
                    name = stem
            if path and (kind in {"file", "source_file", "document"} or not name):
                files.append(
                    SnapshotFile(
                        path=str(path),
                        exists=_bool(node.get("exists"), True),
                        metadata={"graph_node": True, "kind": kind or "file"},
                    )
                )
            if kind not in {"file", "source_file", "document"} and (
                name or qualified or label
            ):
                symbol_name = str(name or qualified or label)
                symbols.append(
                    SnapshotSymbol(
                        name=symbol_name.rsplit(".", 1)[-1],
                        qualified_name=str(qualified or label or symbol_name),
                        file_path=str(path) if path else None,
                        kind=kind or None,
                        exists=_bool(node.get("exists"), True),
                        metadata={"graph_node": True, "id": node.get("id")},
                    )
                )

        # De-duplicate entries while retaining deterministic metadata.
        unique_files: dict[str, SnapshotFile] = {}
        for item in files:
            key = item.path.replace("\\", "/").strip()
            if key:
                unique_files.setdefault(key, item)
        unique_symbols: dict[tuple[str, str], SnapshotSymbol] = {}
        for item in symbols:
            key = (item.qualified_name or item.name, item.file_path or "")
            if item.name.strip():
                unique_symbols.setdefault(key, item)

        status = str(value.get("status") or "").strip().casefold()
        if not status:
            status = "ready" if unique_files or unique_symbols else "partial"
        return cls(
            provider=provider,
            project=str(value.get("project") or value.get("project_name") or "Project_J"),
            root_path=_text(value.get("root_path") or value.get("root") or value.get("workspace")),
            revision=_text(
                value.get("revision")
                or value.get("head_change")
                or value.get("headChange")
                or value.get("change")
            ),
            source_ref=_text(value.get("source_ref") or value.get("sourceRef") or value.get("uri")),
            status=status,
            files=tuple(unique_files.values()),
            symbols=tuple(unique_symbols.values()),
            metadata=dict(value.get("metadata") or {}),
            captured_at=str(value.get("captured_at") or value.get("capturedAt") or utc_now()),
        )

    @classmethod
    def merge(
        cls,
        manifests: Iterable["SnapshotManifest"],
        *,
        project: str = "Project_J",
        root_path: str | None = None,
    ) -> "SnapshotManifest":
        # Provider arrival order is an adapter detail.  Sort before merging so
        # equivalent P4/CodeBaseMemory bundles produce the same composite
        # revision, metadata, and content hash even when transport ordering
        # differs.
        rows = sorted(
            list(manifests),
            key=lambda item: (item.provider, item.manifest_hash),
        )
        if not rows:
            return cls(provider="composite", project=project, root_path=root_path, status="unavailable")
        files: dict[str, SnapshotFile] = {}
        symbols: dict[tuple[str, str], SnapshotSymbol] = {}
        statuses = {item.status for item in rows}
        for manifest in rows:
            for item in manifest.files:
                try:
                    # Local import avoids a module cycle: providers imports
                    # these models, while merge is only called after both
                    # modules have loaded.
                    from .providers import normalize_repo_path

                    canonical_path = normalize_repo_path(
                        item.path,
                        roots=[manifest.root_path] if manifest.root_path else (),
                    )
                except ImportError:  # pragma: no cover - defensive import path
                    canonical_path = item.path.replace("\\", "/").lstrip("/")
                if canonical_path:
                    files.setdefault(
                        canonical_path,
                        SnapshotFile(
                            path=canonical_path,
                            exists=item.exists,
                            head_rev=item.head_rev,
                            have_rev=item.have_rev,
                            head_change=item.head_change,
                            head_time=item.head_time,
                            metadata={**item.metadata, "source_provider": manifest.provider},
                        ),
                    )
            for item in manifest.symbols:
                try:
                    from .providers import normalize_repo_path

                    canonical_symbol_path = normalize_repo_path(
                        item.file_path,
                        roots=[manifest.root_path] if manifest.root_path else (),
                    ) if item.file_path else None
                except ImportError:  # pragma: no cover
                    canonical_symbol_path = item.file_path.replace("\\", "/") if item.file_path else None
                canonical_symbol = SnapshotSymbol(
                    name=item.name,
                    qualified_name=item.qualified_name,
                    file_path=canonical_symbol_path,
                    kind=item.kind,
                    exists=item.exists,
                    metadata={**item.metadata, "source_provider": manifest.provider},
                )
                symbols.setdefault(
                    (canonical_symbol.qualified_name or canonical_symbol.name, canonical_symbol.file_path or ""),
                    canonical_symbol,
                )
        if "ready" in statuses:
            status = "ready"
        elif "partial" in statuses:
            status = "partial"
        elif "failed" in statuses:
            status = "failed"
        else:
            status = "unavailable"
        revisions = [item.revision for item in rows if item.revision]
        return cls(
            provider="composite",
            project=project,
            root_path=root_path or next((item.root_path for item in rows if item.root_path), None),
            revision="+".join(dict.fromkeys(str(item) for item in revisions)) or None,
            source_ref="composite:" + ",".join(item.provider for item in rows),
            status=status,
            files=tuple(files.values()),
            symbols=tuple(symbols.values()),
            metadata={
                "providers": [item.provider for item in rows],
                "provider_statuses": {item.provider: item.status for item in rows},
                "provider_hashes": {item.provider: item.manifest_hash for item in rows},
            },
        )


@dataclass(frozen=True)
class BindingVerification:
    binding_id: str
    status: str
    score: float
    resolved_path: str | None = None
    resolved_symbol: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    applied: bool = False

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError(f"unsupported binding verification status: {self.status}")
        object.__setattr__(self, "score", max(0.0, min(1.0, float(self.score))))

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True)
class VerificationRunResult:
    run_id: str | None
    logical_project_id: str
    mode: str
    status: str
    input_hash: str | None
    provider_names: tuple[str, ...] = ()
    snapshot_ids: tuple[str, ...] = ()
    counts: dict[str, Any] = field(default_factory=dict)
    bindings: tuple[dict[str, Any], ...] = ()
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["provider_names"] = list(self.provider_names)
        value["snapshot_ids"] = list(self.snapshot_ids)
        value["bindings"] = list(self.bindings)
        return value


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value[key] not in (None, ""):
            return value[key]
    return None


def _unwrap_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap common MCP/HTTP envelopes and JSON text blocks."""

    current: Any = dict(value)
    for _ in range(4):
        if isinstance(current, Mapping):
            for key in ("structuredContent", "structured_content", "data", "result"):
                nested = current.get(key)
                if isinstance(nested, Mapping):
                    current = dict(nested)
                    break
            else:
                content = current.get("content")
                if isinstance(content, list) and content:
                    text_items = [
                        item.get("text")
                        for item in content
                        if isinstance(item, Mapping) and isinstance(item.get("text"), str)
                    ]
                    for text in text_items:
                        try:
                            parsed = json.loads(text)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(parsed, Mapping):
                            current = dict(parsed)
                            break
                    else:
                        return dict(current)
                else:
                    return dict(current)
        elif isinstance(current, str):
            try:
                parsed = json.loads(current)
            except (TypeError, json.JSONDecodeError):
                return {"status": "partial", "metadata": {"raw_text": current[:2000]}}
            if isinstance(parsed, Mapping):
                current = dict(parsed)
            else:
                return {"status": "partial"}
        else:
            return {"status": "partial"}
    return dict(current) if isinstance(current, Mapping) else {"status": "partial"}


def _iter_nodes(value: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    nodes = value.get("nodes") or value.get("graph_nodes")
    if isinstance(nodes, Mapping):
        nodes = list(nodes.values())
    if isinstance(nodes, list):
        return [item for item in nodes if isinstance(item, Mapping)]
    return []


def _parse_files(value: Mapping[str, Any]) -> list[SnapshotFile]:
    raw = value.get("files") or value.get("file_entries") or value.get("fileEntries") or []
    if isinstance(raw, Mapping):
        raw = [dict(metadata or {}, path=path) for path, metadata in raw.items()]
    result: list[SnapshotFile] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if isinstance(item, str):
            result.append(SnapshotFile(path=item))
            continue
        if not isinstance(item, Mapping):
            continue
        path = _first(item, "path", "file_path", "filePath", "clientFile", "client_file", "depotFile", "depot_file")
        if not path:
            continue
        result.append(
            SnapshotFile(
                path=str(path),
                exists=_bool(item.get("exists"), True),
                head_rev=_text(_first(item, "head_rev", "headRev", "headRevision")),
                have_rev=_text(_first(item, "have_rev", "haveRev", "haveRevision")),
                head_change=_text(_first(item, "head_change", "headChange", "change")),
                head_time=_text(_first(item, "head_time", "headTime")),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return result


def _parse_symbols(value: Mapping[str, Any]) -> list[SnapshotSymbol]:
    raw = value.get("symbols") or value.get("symbol_entries") or value.get("symbolEntries") or []
    if isinstance(raw, Mapping):
        raw = [dict(metadata or {}, qualified_name=name, name=name) for name, metadata in raw.items()]
    result: list[SnapshotSymbol] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if isinstance(item, str):
            result.append(SnapshotSymbol(name=item.rsplit(".", 1)[-1], qualified_name=item))
            continue
        if not isinstance(item, Mapping):
            continue
        qualified = _first(item, "qualified_name", "qualifiedName", "qualified_symbol", "qualifiedSymbol", "full_name", "fullName")
        name = _first(item, "name", "symbol", "short_name", "shortName") or qualified
        if not name:
            continue
        name_text = str(name)
        result.append(
            SnapshotSymbol(
                name=name_text.rsplit(".", 1)[-1],
                qualified_name=str(qualified) if qualified else name_text,
                file_path=_text(_first(item, "file_path", "filePath", "path", "source_path")),
                kind=_text(item.get("kind") or item.get("type") or item.get("label")),
                exists=_bool(item.get("exists"), True),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return result
