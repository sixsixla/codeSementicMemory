# Phase 2A delivery record

Status: implemented locally on 2026-09-04.

## Delivered slice

Phase 2A turns immutable coding events into auditable candidate memories. The
pipeline is deliberately provider-neutral:

1. `ContextAssembler` reads one bounded task window, removes duplicate event
   ids, preserves order, and hashes the exact selected projection.
2. `ExtractionProvider` returns strict `codememory.extraction_batch.v1` JSON.
   The included `MockLLMProvider` is deterministic and offline; a local or
   hosted LLM can implement the same boundary later.
3. `ExtractionService` checks task/project/session identity, source event ids,
   candidate evidence, and binding evidence before persistence.
4. `ExtractionStore` writes `extraction_runs`, `memory_candidates`, evidence
   rows, relation/link rows, and a rebuildable FTS projection in SQLite.
5. `ExtractionWorker` claims only `event.ingested` jobs, so extraction remains
   outside the synchronous ingest acknowledgement path.

Candidate memories are observations, not stable truth. They may contain route
observations, decisions, failures, validation outcomes, aliases, typed file or
symbol bindings, confidence, uncertainty, and source event ids. Hidden
reasoning and context-compaction records are not persisted.

## Authorized real Codex seed

`fixtures/codex-history/selected-threads.json` is a bounded export of the three
user-authorized local Codex tasks. It retains visible user/assistant/file/tool
evidence, thread ids, titles, host, cwd, item ids, and `partial` completeness;
it intentionally does not claim to be a complete private transcript.

The local application-data database was populated through the normal importer
and ingest service:

- 3 Codex tasks
- 33 canonical events
- 3 successful extraction runs
- 6 candidate memories
- 10 content/artifact records
- 33 `event.ingested` outbox jobs claimed and completed by the extraction worker
- WAL and integrity checks passed

Repeated import and extraction are idempotent for the same input hash. A
forced retry replaces candidate rows only inside the successful SQLite
transaction, preserving the last successful projection if the provider fails.

## Inspection surface

The API adds task, run, candidate, FTS search, and bounded graph endpoints. The
root page serves a local Three.js inspector with task selection, candidate
search, orbit/zoom, node selection, source excerpts, and file/symbol links.
The graph is a rebuildable projection; SQLite event and candidate rows remain
authoritative.

The three static inspector assets are included as package data as well as in the
source checkout, so an editable install is not required for the web surface to
be discovered by the API.

Start it with:

```powershell
py -X utf8 -m codememory serve --db "$env:LOCALAPPDATA\CodeSementicMemory\codememory.sqlite3" --host 127.0.0.1 --port 8876
```

Then open `http://127.0.0.1:8876/`. The Three.js module is loaded from a pinned
CDN URL in this first local preview; the API and data model do not depend on
that frontend choice.

## Verification evidence

- `ruff check src tests` — passed
- `py -m compileall -q src` — passed
- `node --check web/app.js` — passed
- `py -m pytest -q` — 34 passed, one upstream Starlette/httpx deprecation warning
- HTTP smoke — `/v1/health`, `/v1/graph`, and `/` returned 200
- Post-delivery route regression check — `/v1/memories/search?q=NPC` now resolves to
  the static search endpoint (rather than the dynamic candidate-detail route) and
  returns the seeded candidate.

## Explicit next slices

Phase 3 should consolidate repeated candidates into stable cards and maintain
relations with supersede/stale/reject/forgetting rules. Phase 4 should resolve
paths and symbols against repository snapshots and record current validity.
Embeddings/vector search, MCP, and a native Codex adapter remain replaceable
integration layers rather than prerequisites for this SQLite-first core.
