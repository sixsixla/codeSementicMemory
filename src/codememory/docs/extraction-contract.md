# Extraction contract (Phase 2 boundary)

The first round stores evidence only. A future extractor consumes immutable events from `event.ingested` outbox jobs and emits `codememory.extraction_batch.v1` candidates. The schema is already published at [`schemas/extraction_batch.v1.json`](../../../schemas/extraction_batch.v1.json).

An extraction candidate must carry:

- a stable candidate id and kind (`route_observation`, `decision`, `convention`, `failure`, `validation`, `preference`, or `anti_binding`);
- a concise statement and optional aliases;
- zero or more typed code bindings (`public_entry`, `feature_integration`, `supporting_symbol`, `modified_file`, `rejected_candidate`);
- one or more source event ids;
- confidence plus a human-readable uncertainty reason;
- optional lifecycle and relation hints.

The extractor is allowed to propose. It must not directly overwrite a stable memory card. A later consolidator will validate evidence, resolve code entities against a snapshot, merge or supersede candidates, and retain rejected/contradictory observations.
