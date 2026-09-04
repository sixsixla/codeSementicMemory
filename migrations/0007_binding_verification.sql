-- Phase 4B code-binding verification projections.
--
-- Canonical events, candidates, cards, and card versions remain immutable.
-- A snapshot is an externally supplied, content-addressed manifest (for
-- example P4 fstat plus CodeBaseMemory graph output).  Verification rows are
-- append-only per run; the current binding status is only a convenient
-- projection for retrieval/UI and can always be rebuilt from the audit rows.

CREATE TABLE IF NOT EXISTS code_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    logical_project_id TEXT NOT NULL REFERENCES logical_projects(logical_project_id) ON DELETE CASCADE,
    provider TEXT NOT NULL
        CHECK(provider IN ('p4', 'codebase_memory', 'filesystem', 'composite')),
    source_ref TEXT,
    root_path TEXT,
    revision TEXT,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK(status IN ('ready', 'partial', 'unavailable', 'failed')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    UNIQUE(logical_project_id, provider, manifest_hash)
);

CREATE INDEX IF NOT EXISTS idx_code_snapshots_project_time
    ON code_snapshots(logical_project_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_code_snapshots_hash
    ON code_snapshots(manifest_hash);

CREATE TABLE IF NOT EXISTS verification_runs (
    run_id TEXT PRIMARY KEY,
    logical_project_id TEXT NOT NULL REFERENCES logical_projects(logical_project_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('dry_run', 'write')),
    input_hash TEXT NOT NULL,
    provider_names_json TEXT NOT NULL DEFAULT '[]',
    snapshot_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed', 'not_applicable')),
    counts_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_verification_runs_project_time
    ON verification_runs(logical_project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_verification_runs_status
    ON verification_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS binding_verifications (
    verification_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES verification_runs(run_id) ON DELETE CASCADE,
    binding_id TEXT NOT NULL REFERENCES memory_card_bindings(binding_id) ON DELETE CASCADE,
    snapshot_id TEXT NOT NULL REFERENCES code_snapshots(snapshot_id) ON DELETE RESTRICT,
    status TEXT NOT NULL
        CHECK(status IN ('verified', 'missing', 'renamed', 'stale', 'unverified', 'rejected')),
    score REAL NOT NULL CHECK(score >= 0 AND score <= 1),
    resolved_path TEXT,
    resolved_symbol TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    applied INTEGER NOT NULL DEFAULT 0 CHECK(applied IN (0, 1)),
    checked_at TEXT NOT NULL,
    UNIQUE(run_id, binding_id)
);

CREATE INDEX IF NOT EXISTS idx_binding_verifications_binding_time
    ON binding_verifications(binding_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_binding_verifications_snapshot_status
    ON binding_verifications(snapshot_id, status, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_binding_verifications_run_status
    ON binding_verifications(run_id, status);
