# Rebuild Long-Term Memory Around a Mem0-Style Lifecycle

## Goal

Replace the fixed `MemoryKind` model with an extensible, evidence-backed long-term-memory lifecycle that can add, update, merge, delete, supersede, expire, retrieve, and rebuild memories without treating raw conversation events as durable knowledge.

## Product Requirements

1. Memory semantics must be expressed by an extensible dotted `category` such as `user.preference`, `user.constraint`, `ddl.semantic`, or `ddl.metric`; adding a category must not require a database enum migration.
2. Each durable fact must have a stable `memory_key` within its authority scope and category. The key identifies the logical fact while individual rows preserve version history.
3. Durable content must include a machine-readable `content_schema` and a retrieval-ready text representation. Raw user/assistant exchanges remain evidence and are not themselves memory categories.
4. Every candidate must be validated against owned conversation evidence before it can change authoritative memory.
5. Before a write, the system must retrieve the active fact for the same scope, category, and key and produce exactly one decision: `ADD`, `UPDATE`, `MERGE`, `DELETE`, or `NOOP`.
6. A correction or merge must leave an auditable history. The replaced row becomes `SUPERSEDED`, a new active version is created, and the relationship and decision are recorded.
7. Duplicate meaning or identical content must result in `NOOP`; it must not create another active row or trigger unnecessary index writes.
8. Each category must resolve to an explicit policy containing lifecycle, conflict, retrieval, and default-importance rules. Initial lifecycle policies are `PERMANENT`, `ADAPTIVE`, `EXPIRING`, and `FINGERPRINT_BOUND`.
9. Memory state must distinguish `ACTIVE`, `SUPERSEDED`, `EXPIRED`, and `DELETED`. Only currently valid `ACTIVE` rows are recallable.
10. The authority model must retain importance, validity/expiry, access feedback, and optional schema binding through at least `importance_score`, `expires_at`, `last_accessed_at`, `access_count`, and `schema_fingerprint`.
11. Recall must combine semantic/lexical relevance with lifecycle-aware reranking, enforce the user/scope boundary in every store, and verify all index candidates against authoritative MySQL state.
12. MySQL remains authoritative. Elasticsearch and Qdrant remain derived, asynchronously rebuildable projections driven by the existing outbox mechanism.
13. The existing event log, memory links, evidence validation, leasing/retry behavior, and delete-before-purge guarantee must be retained where they still fit the new contracts.
14. User editing and deletion APIs must operate on the new lifecycle model with optimistic concurrency and complete history.
15. Conversation context assembly must recall the new active memory model without changing the separation between permanent MySQL conversation history and temporary Redis/LangGraph checkpoints.

## Rebuild Constraints

- Backward compatibility with `MemoryKind`, its discriminated Pydantic union, old memory rows, old bootstrap DDL, and old ES/Qdrant projection schemas is explicitly not required.
- Do not add the Mem0 SDK, a second authoritative database, an ORM entity layer, a migration framework, another queue, or a generic plugin system.
- Fresh-environment bootstrap SQL and SQLAlchemy Core definitions must describe the same schema.
- The implementation must provide deterministic recreation/reindex tooling or commands for MySQL memory objects, Elasticsearch indexes, and Qdrant collections.
- Planning and implementation may prepare destructive rebuild operations, but no real MySQL table drop/truncate, Elasticsearch index deletion, or Qdrant collection deletion may run until the exact targets are listed and separately approved by the user.
- Initial delivery prioritizes expiry, duplicate `NOOP`, correction/supersession, explicit deletion, fingerprint invalidation, and access statistics. Autonomous low-frequency compression and complex learned decay are out of scope.

## Acceptance Criteria

- [x] No active runtime contract or schema depends on `MemoryKind` or the old fixed memory discriminator union.
- [x] New categories can be introduced through policy configuration/code without changing a database enum.
- [x] The database enforces at most one active version for a scope/category/key and preserves immutable superseded history.
- [x] Candidate processing covers and tests all five decisions: `ADD`, `UPDATE`, `MERGE`, `DELETE`, and `NOOP`.
- [x] Exact duplicates and semantically equivalent candidates do not create duplicate active memories or projection writes.
- [x] User correction creates a new active version, supersedes the prior version, records evidence/events/links, and is immediately reflected by recall.
- [x] Expired and fingerprint-invalid memories cannot be recalled and produce retryable projection deletion work.
- [x] Recall applies tenant/scope/category/status/validity filters in ES/Qdrant and then rechecks candidates in MySQL.
- [x] Recall ranking includes configured category relevance plus importance and lifecycle signals; confirmed recalled rows update access statistics.
- [x] PATCH, DELETE, search, detail, and history APIs expose coherent new-model behavior and return deterministic stale-version/not-found results.
- [x] Conversation extraction remains ordered, leased, retryable, evidence-backed, and safe under concurrent workers.
- [x] Bootstrap SQL, SQLAlchemy schema, ES mappings, Qdrant payload/index definitions, and projection version agree.
- [x] Existing old memory data is not migrated; documented recreation and full reindex paths produce a usable clean environment.
- [x] Unit, repository, API, extraction, lifecycle, projection, and conversation-context tests cover the new model and tenant isolation.
- [x] The repository quality gate passes, with unavailable live dependencies reported rather than treated as successful.

## Source Boundary

- The Mem0 lifecycle concepts are adapted from the user-provided reference: <https://blog.csdn.net/m0_59162559/article/details/153476154>.
- Repository behavior and final contracts are determined by this PRD, the technical design, project specs, and verified source code—not by importing an external product wholesale.
