-- Link immutable event evidence to content-addressed artifact metadata.

CREATE TABLE IF NOT EXISTS event_artifacts (
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL DEFAULT 0 CHECK(ordinal >= 0),
    PRIMARY KEY(event_id, artifact_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_event_artifacts_artifact ON event_artifacts(artifact_id);
