-- Phase 4A continuous quality gate and logical project scope.
-- These tables are additive, replayable projections.  Canonical event and
-- candidate payloads remain immutable and are never rewritten by the gate.

CREATE TABLE IF NOT EXISTS logical_projects (
    logical_project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    identity_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'archived', 'review')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logical_projects_status
    ON logical_projects(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS project_aliases (
    alias_id TEXT PRIMARY KEY,
    raw_project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    logical_project_id TEXT NOT NULL REFERENCES logical_projects(logical_project_id) ON DELETE CASCADE,
    alias_type TEXT NOT NULL
        CHECK(alias_type IN ('root', 'repo', 'basename', 'explicit', 'inferred')),
    alias_value TEXT NOT NULL,
    normalized_root TEXT,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(raw_project_id, logical_project_id, alias_type, alias_value)
);

CREATE INDEX IF NOT EXISTS idx_project_aliases_raw
    ON project_aliases(raw_project_id, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_project_aliases_logical
    ON project_aliases(logical_project_id, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_project_aliases_root
    ON project_aliases(normalized_root);

CREATE TABLE IF NOT EXISTS event_quality_evaluations (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    role TEXT NOT NULL
        CHECK(role IN ('intent', 'code_evidence', 'outcome', 'feedback', 'lifecycle', 'noise', 'unknown')),
    decision TEXT NOT NULL
        CHECK(decision IN ('accepted', 'review', 'quarantine')),
    signal_score REAL NOT NULL CHECK(signal_score >= 0 AND signal_score <= 1),
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    classifier_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_quality_project_decision
    ON event_quality_evaluations(project_id, decision, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_quality_task
    ON event_quality_evaluations(task_id, decision);
CREATE INDEX IF NOT EXISTS idx_event_quality_role
    ON event_quality_evaluations(role, decision);

CREATE TABLE IF NOT EXISTS candidate_quality_reviews (
    candidate_id TEXT PRIMARY KEY REFERENCES memory_candidates(candidate_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL
        CHECK(decision IN ('accepted', 'review', 'quarantine')),
    quality_score REAL NOT NULL CHECK(quality_score >= 0 AND quality_score <= 1),
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    classifier_version TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidate_quality_project_decision
    ON candidate_quality_reviews(project_id, decision, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_quality_task
    ON candidate_quality_reviews(task_id, decision);

CREATE TABLE IF NOT EXISTS quality_runs (
    run_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    logical_project_id TEXT,
    input_hash TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('dry_run', 'write')),
    status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed')),
    counts_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_quality_runs_scope_time
    ON quality_runs(scope, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_quality_runs_status
    ON quality_runs(status, started_at DESC);
