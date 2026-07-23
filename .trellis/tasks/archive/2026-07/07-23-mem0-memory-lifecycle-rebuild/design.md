# Technical Design

## 1. Boundary and Invariants

The rebuild keeps the current infrastructure boundary:

- MySQL is the source of truth for memory versions, evidence, links, events, and projection work.
- Elasticsearch provides lexical/BM25 candidates.
- Qdrant provides vector candidates.
- `memory_index_outbox` is the only projection delivery path.
- Conversation extraction remains asynchronous and evidence-gated.
- Redis/LangGraph checkpoints remain temporary DDL-workflow state and never become long-term-memory authority.

The old `MemoryKind` hierarchy is removed. Semantic category, authority scope, lifecycle policy, and record state become separate concepts.

## 2. Domain Model

### 2.1 Logical identity

`source + nullable user_id + category + memory_key` identifies one logical fact slot.

- Existing `source` identifies the DDL authority or conversation memory source.
- Existing nullable `user_id` distinguishes user-owned facts from DDL facts. Reusing
  these enforced tenant fields avoids a duplicate scope representation.
- `category` is an extensible dotted namespace.
- `memory_key` is stable, deterministic, and category-specific, for example `response.style.verbosity` or `orders.gmv.definition`.

Each update creates a new immutable row/version. A nullable active-slot key (or an equivalent transactionally locked identity table) provides a database-enforced single-active-version invariant without relying only on application checks.

### 2.2 Memory record

The rebuilt `agent_memory` authority includes:

| Field | Purpose |
|---|---|
| `uid` | Unique version identifier |
| `source`, `user_id` | Authority and tenant boundary |
| `category`, `memory_key` | Extensible semantic identity |
| `content`, `memory_text`, `content_schema` | Structured fact and retrieval representation |
| `importance_score` | Policy-normalized importance in `[0, 1]` |
| `lifecycle_policy` | `PERMANENT`, `ADAPTIVE`, `EXPIRING`, or `FINGERPRINT_BOUND` |
| `status` | `ACTIVE`, `SUPERSEDED`, `EXPIRED`, or `DELETED` |
| `valid_from`, `expires_at` | Validity window |
| `schema_fingerprint` | Optional DDL binding |
| `access_count`, `last_accessed_at` | Confirmed-recall feedback |
| `version`, `content_hash` | Optimistic concurrency and duplicate detection |
| timestamps and deletion metadata | Audit and purge control |

Evidence remains in the existing evidence/link structures and points to owned messages or DDL sources. `agent_memory_event` records the decision, actor/source, prior/new version, and normalized reason. `agent_memory_link` records `SUPERSEDES`, evidence, and other supported provenance edges.

### 2.3 Category policy registry

A typed in-process registry maps category prefixes or exact categories to:

- allowed scopes and content schema;
- default lifecycle and importance;
- key derivation/normalization;
- conflict and merge strategy;
- expiry/default TTL rules;
- retrieval eligibility and ranking weights;
- fingerprint requirements.

Unknown categories fail closed during writes. The registry is deliberately small and typed; this is not a plugin framework.

## 3. Write Lifecycle

```text
completed turn
  -> leased extraction job
  -> candidate extraction
  -> exact evidence validation
  -> category policy validation + key normalization
  -> load active same scope/category/key under lock
  -> decide ADD / UPDATE / MERGE / DELETE / NOOP
  -> atomic authority write + event/link/evidence + projection outbox
```

Decision semantics:

- `ADD`: no active logical fact exists; create version 1 as `ACTIVE`.
- `UPDATE`: the candidate corrects/replaces the fact; create a new active version and mark the prior one `SUPERSEDED`.
- `MERGE`: consolidate complementary information into a new active version and supersede all contributing active versions in the same policy-defined slot.
- `DELETE`: mark the active version `DELETED`, emit a delete projection state, and retain authority history until purge conditions are met.
- `NOOP`: preserve authority unchanged and record only the decision telemetry/event required for audit; do not enqueue projection work.

Deterministic checks run before any model-assisted comparison: exact hash duplicate, missing active row, explicit authority deletion, expiry/fingerprint state, and schema validation. A bounded semantic decision step is used only where policy allows ambiguity. Its output is typed and rejected on invalid evidence or identity.

All state transitions, links, and outbox rows commit in one MySQL transaction. The active identity is locked so concurrent workers cannot create two active versions.

## 4. Lifecycle Processing

- `PERMANENT`: no automatic time expiry; explicit correction/deletion still applies.
- `ADAPTIVE`: recall ranking may use policy-specific freshness and access signals, but initial delivery does not autonomously delete low-use facts.
- `EXPIRING`: requires `expires_at`; an expiry worker atomically changes due active rows to `EXPIRED` and emits delete projection states.
- `FINGERPRINT_BOUND`: requires `schema_fingerprint`; a changed authoritative schema invalidates the memory, moves it to `EXPIRED`, and emits delete projection states.

Lifecycle scans use leases/batches and idempotent compare-and-set transitions. A row is never physically purged before all desired DELETE projections are acknowledged. User-wide conversation-data deletion continues to tombstone all eligible user memories before purge.

## 5. Retrieval and Access Feedback

Search flow:

1. Resolve allowed categories and scope from the request/context purpose.
2. Query ES and Qdrant with the same scope/category/status/projection-version predicates.
3. Fuse candidate ranks.
4. Load candidate rows from MySQL and reject wrong scope, non-active state, expired validity, stale fingerprint, version/hash mismatch, or disallowed category.
5. Rerank with normalized relevance, category weight, importance, and lifecycle-specific freshness/access signals.
6. Return the bounded result set and update `access_count`/`last_accessed_at` only for memories actually admitted into context or returned to the caller.

Access updates must not make search correctness depend on telemetry success. They may be batched, but increments must be monotonic and tenant-scoped.

## 6. Public API Shape

Existing endpoint paths may remain, but payloads and responses use the new concepts:

- Search accepts query plus optional category filters and returns category, key, structured content, importance, lifecycle, validity, access metadata, and version.
- Detail/history returns the full logical chain and decision events.
- PATCH supplies expected version and replacement structured content; it executes an explicit `UPDATE` through the same policy and audit path.
- DELETE supplies expected version where applicable and executes `DELETE`; repeated deletion is idempotent.

No public request accepts an internal projection flag or bypasses evidence/policy rules for extraction-generated memory.

## 7. Projection Rebuild

The incompatible authority and projection schema requires a projection-version bump. New ES mappings and Qdrant payload indexes include scope, category, status, authority version, content hash, projection version, lifecycle fields needed for filtering, and retrieval text/vector.

The implementation provides explicit commands/runbook steps to:

1. recreate only the named MySQL memory tables in a disposable or approved environment;
2. recreate the named ES index/alias generation;
3. recreate the named Qdrant collection;
4. enqueue/perform a full projection rebuild from active MySQL rows;
5. verify document/point counts and sampled authority hashes.

No destructive command runs merely because the code or task plan is executed. The exact database, table, index/alias, and collection names must be displayed and separately approved first.

## 8. Failure and Concurrency Behavior

- Invalid evidence/category/schema: reject the candidate and advance/retry according to the existing extraction contract.
- Decision-model failure: retry the extraction job without partial authority writes.
- Concurrent same-key write: one transaction wins; the loser reloads and re-decides or returns `stale_memory` for direct API edits.
- Projection failure: authority remains committed and outbox retry continues.
- Expiry/fingerprint scan failure: active authority remains unchanged until a successful compare-and-set transition.
- Search backend failure: follow existing degraded-search behavior, but never skip MySQL authority validation.

## 9. Compatibility and Rollback

There is no row-level or API-schema compatibility requirement for the old memory model. Rollback is operational rather than migrational: stop new workers/API deployment, restore the previous application plus a database backup, and restore/rebuild the matching projection generation. The destructive rebuild gate must require a recoverable database backup or a confirmed disposable environment.

## 10. Out of Scope

- Importing Mem0 as a runtime dependency.
- Migrating or translating old `MemoryKind` rows.
- Learned autonomous decay/deletion, cross-memory graph reasoning, or automatic low-frequency compaction.
- A generic category plugin marketplace.
- Changing permanent conversation history or Redis checkpoint ownership.
