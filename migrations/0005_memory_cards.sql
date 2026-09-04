-- Phase 3 versioned memory cards and auditable consolidation relations.
-- Candidate rows remain immutable proposals; cards are the governed projection.

CREATE TABLE IF NOT EXISTS memory_cards (
    card_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'route_observation', 'decision', 'convention', 'failure',
        'validation', 'preference', 'anti_binding'
    )),
    canonical_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed', 'verified', 'stable', 'uncertain', 'stale', 'superseded', 'rejected')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    current_version_id TEXT,
    valid_from TEXT,
    valid_until TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, kind, canonical_key)
);

CREATE INDEX IF NOT EXISTS idx_memory_cards_project_status
    ON memory_cards(project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_cards_task
    ON memory_cards(task_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_card_versions (
    card_version_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES memory_cards(card_id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK(version_no > 0),
    statement TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    uncertainty TEXT NOT NULL,
    source_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT,
    valid_until TEXT,
    supersedes_version_id TEXT REFERENCES memory_card_versions(card_version_id) ON DELETE SET NULL,
    change_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(card_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_memory_card_versions_card
    ON memory_card_versions(card_id, version_no DESC);

CREATE TABLE IF NOT EXISTS memory_card_evidence (
    card_version_id TEXT NOT NULL REFERENCES memory_card_versions(card_version_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES memory_candidates(candidate_id) ON DELETE RESTRICT,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    relation_type TEXT NOT NULL DEFAULT 'supports',
    ordinal INTEGER NOT NULL DEFAULT 0 CHECK(ordinal >= 0),
    PRIMARY KEY(card_version_id, candidate_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_card_evidence_candidate
    ON memory_card_evidence(candidate_id);
CREATE INDEX IF NOT EXISTS idx_memory_card_evidence_event
    ON memory_card_evidence(event_id);

CREATE TABLE IF NOT EXISTS memory_card_bindings (
    binding_id TEXT PRIMARY KEY,
    card_version_id TEXT NOT NULL REFERENCES memory_card_versions(card_version_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    path TEXT,
    symbol TEXT,
    qualified_symbol TEXT,
    normalized_target TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unverified'
        CHECK(status IN ('unverified', 'verified', 'stale', 'missing', 'renamed', 'rejected')),
    snapshot_id TEXT,
    evidence_event_ids_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(card_version_id, role, normalized_target)
);

CREATE INDEX IF NOT EXISTS idx_memory_card_bindings_target
    ON memory_card_bindings(normalized_target, status);
CREATE INDEX IF NOT EXISTS idx_memory_card_bindings_path
    ON memory_card_bindings(path);

CREATE TABLE IF NOT EXISTS memory_card_links (
    card_version_id TEXT NOT NULL REFERENCES memory_card_versions(card_version_id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN (
        'card', 'card_version', 'candidate', 'event', 'file', 'symbol', 'task'
    )),
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(card_version_id, target_type, target_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_card_links_target
    ON memory_card_links(target_type, target_id, relation_type);

CREATE TABLE IF NOT EXISTS consolidation_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_key TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL REFERENCES memory_candidates(candidate_id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK(action IN (
        'create', 'merge', 'new_version', 'promote', 'supersede',
        'contradict', 'reject', 'uncertain', 'skip'
    )),
    card_id TEXT REFERENCES memory_cards(card_id) ON DELETE SET NULL,
    card_version_id TEXT REFERENCES memory_card_versions(card_version_id) ON DELETE SET NULL,
    score REAL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_consolidation_decisions_candidate
    ON consolidation_decisions(candidate_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_consolidation_decisions_card
    ON consolidation_decisions(card_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_card_lifecycle_events (
    lifecycle_event_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES memory_cards(card_id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_card_lifecycle_card
    ON memory_card_lifecycle_events(card_id, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_card_fts USING fts5(
    card_id UNINDEXED,
    project_id UNINDEXED,
    task_id UNINDEXED,
    text,
    tokenize='unicode61'
);
