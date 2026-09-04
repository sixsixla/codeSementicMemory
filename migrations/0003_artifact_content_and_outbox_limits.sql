-- Content excerpts and retry policy metadata are projections of immutable events.

ALTER TABLE artifacts ADD COLUMN content_excerpt TEXT;
ALTER TABLE artifacts ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0, 1));
ALTER TABLE outbox ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 8 CHECK(max_attempts > 0);

CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
