"""Code-binding snapshot verification for the Project_J integration scope."""

from .models import (
    BindingVerification,
    SnapshotFile,
    SnapshotManifest,
    SnapshotSymbol,
    VerificationRunResult,
    VERIFICATION_SCHEMA_VERSION,
    VERIFICATION_VERSION,
)
from .providers import (
    CodebaseMemoryManifestProvider,
    FilesystemManifestProvider,
    P4ManifestProvider,
    collect_p4_fstat_manifest,
    normalize_repo_path,
    normalize_symbol,
)
from .service import VerificationService
from .store import VerificationStore

__all__ = [
    "BindingVerification",
    "SnapshotFile",
    "SnapshotManifest",
    "SnapshotSymbol",
    "VerificationRunResult",
    "VERIFICATION_SCHEMA_VERSION",
    "VERIFICATION_VERSION",
    "CodebaseMemoryManifestProvider",
    "FilesystemManifestProvider",
    "P4ManifestProvider",
    "collect_p4_fstat_manifest",
    "normalize_repo_path",
    "normalize_symbol",
    "VerificationService",
    "VerificationStore",
]
