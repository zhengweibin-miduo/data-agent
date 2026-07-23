# Implementation Plan

## Phase A: Contract and Authority Schema

- [x] Inventory every runtime/test/bootstrap reference to `MemoryKind`, old memory payload unions, old status values, and projection fields.
- [x] Define the new category, scope, lifecycle, status, decision, content, policy, and API models.
- [x] Rebuild SQLAlchemy Core memory tables and fresh-environment MySQL bootstrap DDL with a database-enforced single-active-version identity.
- [x] Bump the incompatible projection version and update configuration validation.
- [x] Add schema/model compilation tests before changing behavior.

Review gate: SQLAlchemy DDL and bootstrap SQL agree; no compatibility shim or old discriminator remains.

## Phase B: Policy and Decision Engine

- [x] Implement the typed category-policy registry and initial categories required by existing user and DDL flows.
- [x] Implement normalization, stable key derivation, content hashing, expiry defaults, and fingerprint validation.
- [ ] Implement typed `ADD/UPDATE/MERGE/DELETE/NOOP` decisions with deterministic fast paths and bounded model-assisted comparison.
- [x] Preserve exact evidence ownership validation before decisions are applied.
- [ ] Add focused decision tests for duplicate, correction, complementary merge, explicit deletion, invalid evidence, and concurrent same-key candidates.

Review gate: all decisions are auditable and no decision can create two active versions for one logical identity.

## Phase C: Repository and Lifecycle

- [x] Replace repository auto-supersede behavior with explicit transactional decision application.
- [x] Write version/event/link/evidence/outbox changes atomically.
- [x] Implement optimistic PATCH and idempotent DELETE against the new versions.
- [x] Implement batched expiry and schema-fingerprint invalidation transitions.
- [x] Preserve delete-before-purge acknowledgment behavior and user-data deletion semantics.
- [ ] Add repository integration tests for locking, stale versions, expiry, invalidation, outbox replay, and purge ordering.

Review gate: MySQL alone can explain every active fact and every historical transition.

## Phase D: Projection and Retrieval

- [x] Replace ES mappings and Qdrant payload/index definitions with the new scope/category/lifecycle fields.
- [x] Update outbox projection serialization, replay, delete, and full-rebuild behavior.
- [x] Update exact/BM25/vector/RRF retrieval filters and MySQL post-validation.
- [x] Add lifecycle-aware reranking and confirmed-result access feedback.
- [ ] Add projection and search tests for tenant isolation, stale candidate rejection, expired/deleted exclusion, ranking, and degraded backends.

Review gate: no result can be returned solely because an index says it is active.

## Phase E: Conversation and API Integration

- [x] Update extraction prompts/contracts and worker flow to produce category/key/content candidates rather than old memory kinds.
- [x] Update conversation context assembly to request purpose-appropriate categories.
- [x] Update search/detail/history/PATCH/DELETE API models and handlers.
- [x] Update application composition and settings without exposing internal projection controls.
- [ ] Add end-to-end tests for cross-conversation preference recall, correction, duplicate `NOOP`, expiry, and DDL fingerprint invalidation.

Review gate: permanent conversation data, long-term memory, and Redis checkpoints remain separate stores with separate lifecycles.

## Phase F: Clean Rebuild Operations

- [x] Add or update explicit bootstrap/recreation commands for the named MySQL memory objects, ES index/alias, and Qdrant collection.
- [x] Add a full active-authority projection rebuild and count/hash verification.
- [x] Document the exact destructive targets, backup prerequisite, expected empty-state impact, and rollback procedure.
- [x] Stop before executing any destructive operation and obtain separate user approval for the displayed targets.
- [x] If approved, execute only against the confirmed environment and verify authority/projection consistency.

Review gate: recreation is deterministic and scoped; no wildcard or unresolved environment variable is used for deletion.

### Prepared Destructive Target Manifest (Not Executed)

The current local configuration resolves to these exact application-owned
targets:

- MySQL database: `data_agent`
- MySQL tables: `memory_index_outbox`, `agent_memory_link`,
  `agent_memory_event`, and `agent_memory`
- Elasticsearch index: `data_agent_memory`
- Qdrant collection: `data_agent_memory`

Before execution, the user must confirm the environment and these names after a
recoverable MySQL backup or an explicit declaration that the environment is
disposable. Recreating the MySQL targets empties all authoritative long-term
memory and history; recreating ES/Qdrant empties only derived projections.
Rollback restores the matching MySQL backup and application version, then
recreates and rebuilds both projections from the restored ACTIVE authority.

## Validation

Run from the task worktree:

```bash
uv lock --check
uv run ruff check .
uv run pyright
uv run python -m compileall -q src tests
uv run pytest -m "not integration"
docker compose config
uv run python .trellis/scripts/task.py validate 07-23-mem0-memory-lifecycle-rebuild
git diff --check
```

Also run the repository's SQLAlchemy MySQL DDL compilation/parity checks. Run live MySQL, Redis, ES, Qdrant, TEI, and model-backed tests only when those dependencies are available, and report unavailable dependencies explicitly.

## Rollback Points

- Before authority schema deployment: application rollback only.
- Before destructive recreation: named backups/snapshots plus exact target confirmation are mandatory.
- After authority recreation but before projection rebuild: restore MySQL backup or keep the approved empty authority and rerun bootstrap.
- After projection recreation: ES/Qdrant are disposable and rebuilt from active MySQL authority.
