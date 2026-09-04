# Extraction contract (Phase 2A / Phase 3 input boundary)

Phase 2A consumes a bounded task window assembled from immutable events and
emits `codememory.extraction_batch.v1` candidate observations. The schema is
published at [`schemas/extraction_batch.v1.json`](../../../schemas/extraction_batch.v1.json).

`ContextAssembler` is deterministic: it de-duplicates event ids, preserves
chronological order, bounds event/text size, and hashes the exact selected
projection. The hash plus `(task_id, extractor_version, prompt_version,
schema_version)` is the extraction idempotency key.

The provider boundary is intentionally small. `MockLLMProvider` is an offline
heuristic used by the CLI/API and tests; a local or cloud LLM can implement the
same `extract(context)` protocol later. Provider work runs only after an event
has been acknowledged and is normally driven by `ExtractionWorker` claiming
`event.ingested` outbox jobs.

An extraction candidate must carry:

- a stable candidate id and kind (`route_observation`, `decision`, `convention`, `failure`, `validation`, `preference`, or `anti_binding`);
- a concise statement and optional aliases;
- zero or more typed code bindings (`public_entry`, `feature_integration`, `supporting_symbol`, `modified_file`, `rejected_candidate`);
- one or more source event ids;
- confidence plus a human-readable uncertainty reason;
- optional lifecycle and relation hints.

The extractor is allowed to propose. It must not directly overwrite a stable
memory card. `ExtractionService` rejects batches that change the task/project,
reference unknown event ids, omit candidate evidence, or attach a binding to
evidence outside that candidate. A successful run stores the original input
hash, provider/model/prompt/schema metadata, result hash, candidate JSON, and
normalized evidence/link rows in SQLite. Candidate evidence must be declared
by the batch and resolve to the assembled event window. A failed run stores an
error and can be retried without mutating canonical events; a forced retry
keeps the prior candidate projection until the replacement transaction
succeeds. If that projection is already referenced by a card, the old
candidates remain immutable and the retry is stored as a collision-safe new
observation with an explicit `supersedes` edge.

Candidate rows are observations, not final truth. The implemented Phase 3
consolidator validates and merges them into versioned cards, preserves
contradictions as new versions or uncertain cards, and applies guarded
supersede/stale/reject transitions. Phase 4 will resolve paths/symbols against a
repository snapshot. Embeddings and vector indexes remain optional projections.

## History provenance

`CodexHistoryImporter` accepts a bounded `read_thread`-compatible JSON export.
Visible user/assistant/file/tool records become `codememory.event.v1` events;
reasoning/context-compaction items are intentionally omitted. Each event keeps
`codex_thread_id`, host, sanitized title, turn/item id, source, cwd, optional
history file-path hints, and an explicit `completeness` value. A `read_thread`
page marked `newest_first` is normalized to chronological `seq` order before
ingest. The checked-in seed at
`fixtures/codex-history/selected-threads.json` is intentionally marked
`partial`; it is reproducible test input, not a claim that the full private
Codex transcript was copied.

## Graph projection

`ExtractionStore.graph()` derives UI nodes and links from SQLite facts rather
than making the graph authoritative. It connects task → session → event,
candidate → supporting event, and candidate → file/symbol bindings.
`CardStore.extend_graph()` adds versioned card, binding, and lifecycle
relations while filtering edges whose endpoints are outside the bounded node
set. The local Three.js page at `/` consumes only `GET /v1/graph` and the
candidate/card APIs; it can be discarded and rebuilt without changing the fact
store.
