# Research: adapting usememos/memos for LLM memory

- Repository: [`usememos/memos`](https://github.com/usememos/memos)
- Inspected commit: `469c995cc04b5e7de259156d28c58b948e85d111`
- Date: 2026-07-17
- Scope: long-term structured LLM memory for the DDL metadata workflow

## Important boundary

Memos is a note application, not an LLM memory or LangGraph recovery system.
The useful reference is its durable content model: one canonical record,
rebuildable payload, typed relations, state/pinning, and repository boundaries.
Redis checkpoints, semantic validation, model-version compatibility, trust,
and transactional Meta synchronization remain project-specific additions.
The adapted memory tables are application-owned and belong in a configurable
MySQL database outside Meta. Cross-database atomicity is preserved by
schema-qualifying both tables on the existing engine and Session.

## Source-backed patterns

### Canonical memo record

[`store/memo.go`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/store/memo.go)
defines one memo with system ID, stable user-facing UID, row status, creator,
timestamps, canonical content, visibility, pinning, and payload. Find requests
centralize IDs, status, visibility, filters, pagination, and ordering.

The MySQL schema in
[`store/migration/mysql/LATEST.sql`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/store/migration/mysql/LATEST.sql)
persists those fields and makes `uid` unique.

Adaptation:

- use one stable UID per accepted semantic memory;
- keep canonical typed decision content separate from derived retrieval data;
- retain created/updated timestamps, `NORMAL`/`ARCHIVED`, and pinning;
- add LLM-specific source, kind, scope key, schema fingerprint, trust, and
  model/prompt/graph versions.

### Rebuildable payload

[`proto/store/memo.proto`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/proto/store/memo.proto)
stores tags and calculated content properties in `MemoPayload`.

[`server/runner/memopayload/runner.go`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/server/runner/memopayload/runner.go)
rebuilds that payload from canonical content, processes 100 records per batch,
logs per-record errors, continues the batch, and reports counts. Create/update
paths also rebuild payload when content changes in
[`server/router/api/v1/memo_service.go`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/server/router/api/v1/memo_service.go).

Adaptation:

- canonical JSON contains validated decisions, concise evidence, and explicit
  user-confirmed definitions;
- payload contains rebuildable tags, identities, fingerprints, trust, and
  content/model/prompt/graph versions;
- stale payload can be rebuilt in bounded batches without changing accepted
  meaning;
- a payload rebuild failure excludes that memory from automatic reuse but does
  not destroy canonical content.

### Typed relations

[`store/memo_relation.go`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/store/memo_relation.go)
defines `REFERENCE` and `COMMENT` relation types. The MySQL schema has a unique
`(memo_id, related_memo_id, type)` key.

[`store/db/mysql/memo_relation.go`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/store/db/mysql/memo_relation.go)
implements duplicate-safe upsert plus source/target/bidirectional batch query.
`store.DeleteMemo` explicitly removes inbound/outbound relations before
deleting a memo.

Adaptation:

- `COMMENT` links a user answer to the LLM question;
- `REFERENCE` links accepted decisions to table/column/question/answer
  memories;
- add project-specific `SUPERSEDES` so correction preserves history while
  exactly one compatible active decision is retrieved;
- keep the unique relation triple and batch-load relations to avoid N+1.

### Archive, pin, update, and retrieval

[`store/common.go`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/store/common.go)
defines `NORMAL` and `ARCHIVED`. Memos default list behavior excludes archived
records, supports pinning/order/filter/page controls, and allows archive through
the update state.

[`store/db/mysql/memo.go`](https://github.com/usememos/memos/blob/469c995cc04b5e7de259156d28c58b948e85d111/store/db/mysql/memo.go)
owns bound create/list/update/delete SQL, optional content exclusion, filtering,
stable ordering with ID tie-breaker, and pagination. The API batch-loads
relations/attachments/reactions for memo lists rather than issuing per-row
queries.

Adaptation:

- retrieve `NORMAL` exact source/scope/fingerprint matches by default;
- pinned user-confirmed definitions outrank model-only memories;
- correction creates a new memory, adds `SUPERSEDES`, and archives the old
  memory instead of rewriting accepted history;
- expose bounded content projection and batch relation loading.

### Layering and failure behavior

Memos separates API service, store domain facade, and database driver. Store
methods validate stable UIDs and own relation/attachment cleanup.

One Memos behavior must not be copied: API memo creation persists the memo
before attachments and relations, so a later relation failure can leave the
base memo created. This DDL workflow requires stronger atomicity: accepted Meta
rows, memory records, relations, and supersession/archive updates must use one
`MysqlClientManager.session()` transaction.

## Recommended project schema

`memory.database` defaults to `data_agent`; the names below are qualified with
that validated database identifier and are not part of the Meta schema.

```text
<memory.database>.llm_memory
  id, uid UNIQUE, source, kind, scope_key, schema_fingerprint,
  row_status, pinned, content JSON, payload JSON, content_version,
  created_at, updated_at

<memory.database>.llm_memory_relation
  memory_id, related_memory_id, relation_type,
  UNIQUE(memory_id, related_memory_id, relation_type)
```

Initial memory kinds:

- `SEMANTIC_DECISION`
- `METRIC_QUESTION`
- `USER_ANSWER`
- `METRIC_DEFINITION`

Do not store raw prompts, chain-of-thought, unbounded transcripts, stack traces,
or rejected/incomplete model output as reusable semantic memory.

## Retrieval and invalidation

1. Parse current DDL first; the AST remains authoritative.
2. Query by source, scope key, schema fingerprint, kind, and `NORMAL`.
3. Parse canonical content through the current Pydantic model.
4. Verify every referenced table/column against the AST.
5. Prefer pinned user-confirmed memory, then compatible model-only memory.
6. Pass a bounded typed capsule, not all history, to the LLM.
7. Reuse unchanged decisions; regenerate or ask only for changed/missing
   meaning.
8. If compatible active memories conflict, ask the user or reject.

Qdrant is unnecessary for the first release because current retrieval has exact
identities and fingerprints. If semantic search is later required, Qdrant is a
rebuildable projection of active memory summaries; MySQL stays authoritative.

## Exception recovery

- Active candidates live in the Redis/LangGraph checkpoint until final
  acceptance. Failed/rejected/expired jobs create no trusted memory.
- Accepted Meta plus memories/relations commit in one MySQL transaction; any
  error rolls all of them back.
- Deterministic memory UIDs and the unique relation triple make replay safe
  after a crash between MySQL commit and graph checkpoint acknowledgement.
- A stale/incompatible payload is rebuilt from canonical content.
- Corrupt canonical content is excluded and regenerated from current DDL; it
  never overrides AST facts.
- Supersession/archive occurs only when the replacement succeeds, so a failed
  correction leaves the previous accepted memory active.
- Permit one active job per logical source initially to prevent an older job
  from committing stale memory after a newer job. Upgrade to finer-grained
  leases only when measured throughput requires it.
- Normal archive preserves audit history. Explicit hard deletion must remove
  inbound/outbound relations safely.

## Resolved product decision

Long-term LLM memories are browser-visible and manageable through bounded list,
detail, archive, pin/unpin, and structured correction APIs. The project does
not copy Memos' arbitrary notes, user-facing comments, social features,
attachments, reactions, or hard-delete surface.

A correction creates a user-confirmed replacement, supersedes/archives the old
memory, and requires the source DDL to be reprocessed before Meta changes. This
preserves the workflow's full-validation and atomic-snapshot boundary.
