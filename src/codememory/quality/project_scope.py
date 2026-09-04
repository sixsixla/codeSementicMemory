"""Logical project identity resolution without rewriting raw project ids."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


CODEMEMORY_ROOT_NAMES = frozenset(
    {
        "codesementicmemory",
        "codesemanticmemory",
        "codememory",
    }
)
_EXTERNAL_PROJECT_RE = re.compile(
    r"(?i)(?:\bproject[\s_-]*j\b|项目[\s_-]*j\b|"
    r"(?:p4workspace|workspace_release)[\\/][^\s`\"'<>|()\[\]，。；：、]*project[\s_-]*j)"
)


def normalize_root(value: Any) -> str:
    """Normalize Windows/POSIX roots for deterministic alias comparison."""

    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    # Preserve a drive prefix while collapsing duplicate separators and dot
    # segments.  ``PurePosixPath`` also behaves consistently on non-Windows
    # test runners where the product still stores Windows paths.
    raw = re.sub(r"/+", "/", raw)
    drive = ""
    match = re.match(r"^([a-zA-Z]:)(/.*)?$", raw)
    if match:
        drive = match.group(1).casefold()
        raw = match.group(2) or "/"
    try:
        normalized = str(PurePosixPath(raw))
    except (TypeError, ValueError):
        normalized = raw
    if normalized == ".":
        normalized = ""
    normalized = normalized.rstrip("/") or ("/" if raw.startswith("/") else "")
    if drive:
        normalized = drive + (normalized if normalized.startswith("/") else "/" + normalized)
    return normalized.casefold()


def root_basename(value: Any) -> str:
    normalized = normalize_root(value).rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def is_codememory_root(value: Any) -> bool:
    """Return whether a root denotes this memory repository itself."""

    return root_basename(value) in CODEMEMORY_ROOT_NAMES


def has_external_project_reference(
    root_path: Any,
    text: Any = "",
    paths: Any = (),
) -> bool:
    """Detect an external Project_J reference inside CodeMemory work.

    The raw event/project identity remains authoritative.  This helper only
    emits a quality signal when a task scoped to the CodeSementicMemory
    repository talks about the separately checked-out Project_J codebase.  It
    deliberately does not infer a mismatch for ordinary Project_J events whose
    own root is a Project_J checkout.
    """

    if not is_codememory_root(root_path):
        return False
    values = [str(text or "")]
    if isinstance(paths, (list, tuple, set, frozenset)):
        values.extend(str(item or "") for item in paths)
    elif paths:
        values.append(str(paths))
    return bool(_EXTERNAL_PROJECT_RE.search("\n".join(values)))


def logical_identity_key(
    *,
    root_path: str | None = None,
    repo_id: str | None = None,
    display_name: str | None = None,
    infer_common_name: bool = True,
) -> str:
    """Return a conservative, deterministic identity key.

    A repository id is strongest.  For repeated checkouts of a well-known
    project name (notably ``Project_J``), the common-name key intentionally
    allows an auditable alias to unify roots while retaining each raw project
    id.  Other roots stay isolated until an explicit alias is registered.
    """

    normalized = normalize_root(root_path)
    basename = root_basename(normalized)
    if infer_common_name and basename in {"project_j", "projectj"}:
        return "name:project_j"
    if repo_id and str(repo_id).strip():
        return f"repo:{str(repo_id).strip().casefold()}"
    if normalized:
        return f"root:{normalized}"
    name = str(display_name or "unknown").strip().casefold() or "unknown"
    return f"name:{name}"


def logical_project_id(identity_key: str) -> str:
    digest = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:24]
    return f"logical-{digest}"


@dataclass(frozen=True)
class ScopeResolution:
    raw_project_id: str
    logical_project_id: str
    identity_key: str
    display_name: str
    normalized_root: str
    confidence: float
    alias_type: str
    inferred: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_project_id": self.raw_project_id,
            "logical_project_id": self.logical_project_id,
            "identity_key": self.identity_key,
            "display_name": self.display_name,
            "normalized_root": self.normalized_root,
            "confidence": self.confidence,
            "alias_type": self.alias_type,
            "inferred": self.inferred,
        }
