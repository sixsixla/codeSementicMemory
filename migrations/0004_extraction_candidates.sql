-- Phase 2A extraction runs and candidate memories.
-- Candidates are proposals backed by immutable events; Phase 3 owns promotion
-- to stable cards and lifecycle reconciliation.

CREATE TABLE IF NOT EXISTS extraction_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    session_id TEXT,
    input_hash TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'succeeded', 'retry', 'dead', 'skipped')),
    input_json TEXT NOT NULL,
    result_json TEXT,
    result_hash TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, input_hash, extractor_version, prompt_version, schema_version)
);

CREATE INDEX IF NOT EXISTS idx_extraction_runs_task_time
    ON extraction_runs(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_runs_status
    ON extraction_runs(status, updated_at);

CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES extraction_runs(run_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    session_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN (
        'route_observation', 'decision', 'convention', 'failure',
        'validation', 'preference', 'anti_binding'
    )),
    statement TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    bindings_json TEXT NOT NULL DEFAULT '[]',
    evidence_event_ids_json TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    uncertainty TEXT NOT NULL,
    lifecycle_hint TEXT NOT NULL DEFAULT 'candidate'
        CHECK(lifecycle_hint IN ('observed', 'candidate', 'verified', 'stable', 'stale', 'superseded', 'rejected')),
    relation_hints_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_task_time
    ON memory_candidates(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_kind
    ON memory_candidates(task_id, kind);

CREATE TABLE IF NOT EXISTS memory_candidate_evidence (
    candidate_id TEXT NOT NULL REFERENCES memory_candidates(candidate_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(candidate_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_candidate_evidence_event
    ON memory_candidate_evidence(event_id);

CREATE TABLE IF NOT EXISTS memory_candidate_links (
    candidate_id TEXT NOT NULL REFERENCES memory_candidates(candidate_id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN ('event', 'artifact', 'file', 'symbol', 'candidate', 'task')),
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(candidate_id, target_type, target_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_candidate_links_target
    ON memory_candidate_links(target_type, target_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_candidate_fts USING fts5(
    candidate_id UNINDEXED,
    project_id UNINDEXED,
    task_id UNINDEXED,
    text,
    tokenize='unicode61'
);
