"""Provider-neutral snapshot indexes and the optional Project_J P4 bridge."""

from __future__ import annotations

import posixpath
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import SnapshotFile, SnapshotManifest, SnapshotSymbol


_DRIVE_RE = re.compile(r"^[a-zA-Z]:/")
_PROJECT_MARKERS = (
    "/game_client/project_j/",
    "/project_j/",
    "game_client/project_j/",
    "project_j/",
)


def normalize_repo_path(value: Any, *, roots: Iterable[str] = ()) -> str:
    """Normalize absolute/relative Windows, depot, and URI paths.

    The CodeBaseMemory graph reports paths relative to ``Project_J`` while P4
    often reports a client or depot path.  The marker stripping is intentionally
    conservative and only removes known Project_J roots; unknown absolute
    paths remain visible for diagnostics instead of being guessed away.
    """

    raw = str(value or "").strip().strip('"\'')
    if not raw:
        return ""
    if raw.casefold().startswith("file://"):
        raw = raw[7:]
    raw = raw.replace("\\", "/")
    raw = re.sub(r"/+(?=/)", "/", raw)
    # Remove a configured client/workspace root first.
    normalized_roots = sorted(
        (str(root or "").strip().replace("\\", "/").rstrip("/") for root in roots),
        key=len,
        reverse=True,
    )
    lowered = raw.casefold()
    for root in normalized_roots:
        if not root:
            continue
        root_lower = root.casefold()
        if lowered == root_lower:
            return ""
        prefix = root_lower + "/"
        if lowered.startswith(prefix):
            raw = raw[len(root) + 1 :]
            lowered = raw.casefold()
            break

    # Known Project_J path markers make P4 client/depot paths and absolute
    # event paths comparable to a CodeBaseMemory ``Assets/...`` path.
    for marker in _PROJECT_MARKERS:
        index = lowered.find(marker)
        if index >= 0:
            raw = raw[index + len(marker) :]
            lowered = raw.casefold()
            break
    if lowered.startswith("./"):
        raw = raw[2:]
    raw = raw.lstrip("/")
    if not raw:
        return ""
    # ``normpath`` is POSIX by design so the result is stable on all hosts.
    raw = posixpath.normpath(raw)
    if raw in {".", ".."} or raw.startswith("../"):
        return ""
    # A path that still contains a drive is an unknown external root; retain
    # it in canonical form rather than silently treating it as a repo path.
    return raw


def path_key(value: Any, *, roots: Iterable[str] = ()) -> str:
    return normalize_repo_path(value, roots=roots).casefold()


def normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.removeprefix("global::")
    raw = raw.replace("::", ".").replace("/", ".")
    raw = re.sub(r"\s+", "", raw)
    raw = raw.rstrip(";")
    # Calls and generic arity are not part of a stable binding identity.
    raw = re.sub(r"\([^)]*\)$", "", raw)
    return raw.strip(".")


def symbol_key(value: Any) -> str:
    return normalize_symbol(value).casefold()


@dataclass(frozen=True)
class PathMatch:
    entry: SnapshotFile | None
    normalized_path: str

    @property
    def found(self) -> bool:
        return bool(self.entry and self.entry.exists)


@dataclass(frozen=True)
class SymbolMatch:
    entry: SnapshotSymbol | None
    normalized_symbol: str
    match_type: str = "none"

    @property
    def found(self) -> bool:
        return bool(self.entry and self.entry.exists)


class ManifestIndex:
    """Lookup index shared by P4 and CodeBaseMemory manifests."""

    def __init__(self, manifest: SnapshotManifest):
        roots = [manifest.root_path] if manifest.root_path else []
        self.manifest = manifest
        self.roots = tuple(item for item in roots if item)
        self.files: dict[str, list[SnapshotFile]] = defaultdict(list)
        self.symbols: dict[str, list[SnapshotSymbol]] = defaultdict(list)
        self.short_symbols: dict[str, list[SnapshotSymbol]] = defaultdict(list)
        for item in manifest.files:
            key = path_key(item.path, roots=self.roots)
            if key:
                self.files[key].append(item)
        for item in manifest.symbols:
            qualified = symbol_key(item.qualified_name or item.name)
            short = symbol_key(item.name.rsplit(".", 1)[-1])
            if qualified:
                self.symbols[qualified].append(item)
            if short:
                self.short_symbols[short].append(item)

    def lookup_path(self, value: Any) -> PathMatch:
        normalized = normalize_repo_path(value, roots=self.roots)
        candidates = self.files.get(normalized.casefold(), []) if normalized else []
        # Prefer an existing entry when duplicate provider records include a
        # tombstone and a current revision.
        entry = next((item for item in candidates if item.exists), candidates[0] if candidates else None)
        return PathMatch(entry=entry, normalized_path=normalized)

    def lookup_symbol(self, value: Any, qualified: Any = None) -> SymbolMatch:
        requested_qualified = normalize_symbol(qualified or "")
        requested = normalize_symbol(value)
        exact_key = symbol_key(requested_qualified or requested)
        candidates = self.symbols.get(exact_key, []) if exact_key else []
        if candidates:
            entry = next((item for item in candidates if item.exists), candidates[0])
            return SymbolMatch(entry=entry, normalized_symbol=requested_qualified or requested, match_type="qualified")
        short = requested.rsplit(".", 1)[-1] if requested else requested_qualified.rsplit(".", 1)[-1]
        short_candidates = self.short_symbols.get(symbol_key(short), []) if short else []
        existing = [item for item in short_candidates if item.exists]
        # A short-name match is safe only when the index has one unique source
        # path.  Ambiguous symbols must remain unverified instead of guessing.
        paths = {path_key(item.file_path, roots=self.roots) for item in existing if item.file_path}
        if len(existing) == 1 or (existing and len(paths) == 1):
            return SymbolMatch(
                entry=existing[0],
                normalized_symbol=requested_qualified or requested,
                match_type="short",
            )
        return SymbolMatch(entry=None, normalized_symbol=requested_qualified or requested)

    def resolve(
        self,
        *,
        path: Any = None,
        symbol: Any = None,
        qualified_symbol: Any = None,
    ) -> dict[str, Any]:
        path_match = self.lookup_path(path) if path else PathMatch(None, "")
        symbol_match = (
            self.lookup_symbol(symbol, qualified_symbol)
            if (symbol or qualified_symbol)
            else SymbolMatch(None, "")
        )
        return {
            "provider": self.manifest.provider,
            "snapshot_status": self.manifest.status,
            "symbol_count": self.manifest.symbol_count,
            "symbol_coverage": str(self.manifest.metadata.get("symbol_coverage") or "none").casefold(),
            "path": {
                "requested": str(path or ""),
                "normalized": path_match.normalized_path,
                "found": path_match.found,
                "entry": path_match.entry.as_dict() if path_match.entry else None,
            },
            "symbol": {
                "requested": str(qualified_symbol or symbol or ""),
                "normalized": symbol_match.normalized_symbol,
                "found": symbol_match.found,
                "match_type": symbol_match.match_type,
                "entry": symbol_match.entry.as_dict() if symbol_match.entry else None,
            },
        }


class ManifestProvider:
    """A provider adapter backed by a normalized snapshot manifest."""

    provider_name = "filesystem"

    def __init__(self, manifest: SnapshotManifest | Mapping[str, Any]):
        self.manifest = SnapshotManifest.from_mapping(manifest, provider_override=self.provider_name)
        self.index = ManifestIndex(self.manifest)

    @property
    def available(self) -> bool:
        return self.manifest.status in {"ready", "partial"} and bool(
            self.manifest.files or self.manifest.symbols
        )

    def resolve(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        return self.index.resolve(
            path=binding.get("path"),
            symbol=binding.get("symbol"),
            qualified_symbol=binding.get("qualified_symbol"),
        )


class P4ManifestProvider(ManifestProvider):
    provider_name = "p4"


class CodebaseMemoryManifestProvider(ManifestProvider):
    provider_name = "codebase_memory"


class FilesystemManifestProvider(ManifestProvider):
    provider_name = "filesystem"


def _parse_fstat_records(output: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in str(output or "").splitlines():
        line = line.rstrip("\r")
        if not line.startswith("... "):
            continue
        payload = line[4:]
        if " " not in payload:
            continue
        key, value = payload.split(" ", 1)
        # ztag emits a new record at every depotFile.  A few server versions
        # use ``depotFile0`` for batch output, so accept the numeric suffix.
        if key.casefold().startswith("depotfile") and current:
            records.append(current)
            current = {}
        current[key] = value.strip()
    if current:
        records.append(current)
    return records


def collect_p4_fstat_manifest(
    paths: Sequence[str],
    *,
    root_path: str | None = None,
    port: str | None = None,
    user: str | None = None,
    client: str | None = None,
    executable: str = "p4",
    timeout_seconds: int = 30,
    runner: Callable[..., Any] = subprocess.run,
) -> SnapshotManifest:
    """Collect a bounded, read-only P4 manifest for explicit Project_J paths.

    No P4 command is run implicitly by the memory service.  Callers must
    provide the path set and connection options; this keeps credentials and
    network policy outside the SQLite core.  The function is easy to test by
    injecting a runner and is also useful to a local Project_J adapter.
    """

    selected = [str(item).strip() for item in paths if str(item).strip()]
    if not selected:
        # Never invoke ``p4 fstat`` without an explicit path set: depending
        # on client context that can expand to the whole workspace.  Returning
        # an unavailable bounded snapshot keeps the adapter read-only and
        # makes the missing input visible to the caller.
        return SnapshotManifest(
            provider="p4",
            root_path=root_path,
            status="unavailable",
            source_ref="p4:fstat",
            metadata={
                "path_count": 0,
                "coverage": "selected_paths",
                "error_type": "no_paths",
                "error": "at least one explicit path is required",
            },
        )
    command = [executable]
    if port:
        command.extend(["-p", str(port)])
    if user:
        command.extend(["-u", str(user)])
    if client:
        command.extend(["-c", str(client)])
    command.extend(
        [
            "-ztag",
            "fstat",
            "-Ol",
            "-T",
            "depotFile,clientFile,headRev,haveRev,headChange,headTime",
            *selected,
        ]
    )
    metadata: dict[str, Any] = {
        "command": command,
        "path_count": len(selected),
        "coverage": "selected_paths",
    }
    try:
        completed = runner(
            command,
            cwd=root_path or None,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        metadata.update({"error_type": type(exc).__name__, "error": str(exc)[:1000]})
        return SnapshotManifest(
            provider="p4",
            root_path=root_path,
            status="unavailable",
            source_ref="p4:fstat",
            metadata=metadata,
        )

    returncode = int(getattr(completed, "returncode", 1) or 0)
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    records = _parse_fstat_records(stdout)
    files: list[SnapshotFile] = []
    for record in records:
        path = record.get("clientFile") or record.get("depotFile")
        if not path:
            continue
        files.append(
            SnapshotFile(
                path=path,
                exists=True,
                head_rev=record.get("headRev"),
                have_rev=record.get("haveRev"),
                head_change=record.get("headChange"),
                head_time=record.get("headTime"),
                metadata={"depot_file": record.get("depotFile")},
            )
        )
    metadata["returncode"] = returncode
    metadata["stderr"] = stderr[-2000:] if stderr else ""
    if returncode != 0 and not files:
        status = "unavailable"
    elif files and returncode == 0:
        status = "ready"
    elif files:
        status = "partial"
    else:
        status = "partial"
    revisions = [item.head_change for item in files if item.head_change]
    return SnapshotManifest(
        provider="p4",
        root_path=root_path,
        revision="+".join(dict.fromkeys(revisions)) or None,
        status=status,
        source_ref="p4:fstat",
        files=tuple(files),
        metadata=metadata,
    )


def provider_from_manifest(
    manifest: SnapshotManifest | Mapping[str, Any],
    *,
    provider: str | None = None,
) -> ManifestProvider:
    normalized = SnapshotManifest.from_mapping(manifest, provider_override=provider)
    if normalized.provider == "p4":
        return P4ManifestProvider(normalized)
    if normalized.provider == "codebase_memory":
        return CodebaseMemoryManifestProvider(normalized)
    if normalized.provider == "filesystem":
        return FilesystemManifestProvider(normalized)
    # A composite manifest is still a normal index; use the generic provider
    # while retaining its provider name in evidence.
    return ManifestProvider(normalized)
