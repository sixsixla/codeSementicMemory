# Event protocol notes

`codememory.event.v1` is the only write contract in the first round. An agent adapter maps native callbacks to an envelope and sends it to `IngestService`; it does not classify memories.

## Required identity

`event_id` is the producer's stable event identity. `external_event_id` can carry a native sequence/id. `producer.agent_id` plus `producer.adapter` scopes external identities. `session_id` plus `seq` protects replayed session order. A repeated identity with the same canonical hash is a duplicate; a different hash is a conflict.

## Evidence shape

Use `event_type` to describe what happened, keep routing fields in the envelope, and put adapter-specific details in `context` or `payload`. `artifacts` are references, not an invitation to embed an unbounded repository. Content is bounded by `CODEMEMORY_ARTIFACT_MAX_BYTES`; the stored metadata records the full size and `truncated` state.

## Ordering and durability

Parent links are nullable text because streams can be late or out of order. One SQLite transaction writes the event, entity upserts, artifact links, search projection, and `event.ingested` outbox job. The HTTP response is sent only after that transaction commits.

## Privacy

The ingress redactor runs before hashing and persistence. It currently covers common key/token/authorization/password assignments, bearer values, and OpenAI-style `sk-` values. Deployments should add a project-specific ruleset before capturing production conversations.
