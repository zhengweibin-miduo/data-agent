---
goal: Refactor accepted snapshot publication and Meta Projection into DDL Metadata-owned deep modules with public seam tests
version: 1.0
date_created: 2026-08-02
last_updated: 2026-08-02
owner: zwb
status: Planned
tags: [architecture, ddd, metadata, projection, tdd]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan executes a hard internal package migration and two vertical seam refactors. It preserves the accepted-snapshot transaction and all projection convergence contracts while replacing implementation-coupled tests after equivalent public behavior is covered.

## 1. Requirements & Constraints

- **REQ-001**: DDL Metadata must own Meta Projection; active code must not retain the top-level `metadata_indexing` package.
- **REQ-002**: Accepted snapshot publication must expose one deep application interface and atomically commit Meta, Memory, Data Sync desired state, and projection outbox under generation locks.
- **REQ-003**: Meta Projection domain and application code must separate deterministic policy from MySQL, Elasticsearch, Qdrant, TEI, settings, and client construction.
- **REQ-004**: Meta Projection must expose a neutral, transaction-participating value-change input for the dependent Data Sync refactor.
- **REQ-005**: Projection dispatch must preserve short claim, remote work outside row locks, short settle, full authority identity, retry/defer/dead-letter, rebuild, and resumability behavior.
- **REQ-006**: Search must preserve bounded retrieval and authoritative Meta readback, including concurrent refresh generation detection.
- **CON-001**: Do not change database schemas, external indexes, HTTP/SSE contracts, Redis/LangGraph contracts, configuration keys, or log event names.
- **CON-002**: Do not add database, vector-index, or historical-data migration, dual-write, or cleanup paths.
- **CON-003**: Do not activate the dependent Data Sync task until this task's projection input interface is reviewed.
- **GUD-001**: Apply red-green-replace vertically; remove old private-helper and duplicate tests only after the new public seam proves the same behavior.
- **PAT-001**: Create ports only for independently variable collaborators with leverage; avoid a repository-shaped UnitOfWork protocol and empty architectural layers.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Establish package ownership and pure contracts without changing runtime behavior.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add failing package-boundary tests that require Meta Projection to live under DDL Metadata and forbid domain/application imports of Data Sync tables, SQLAlchemy, global settings, and external SDKs. |  |  |
| TASK-002 | Move projection models and deterministic desired-version, normalization, cursor, generation, and budget policies into `ddl_metadata.meta_projection.domain`; update tests through public policy names. |  |  |
| TASK-003 | Hard-move the remaining `metadata_indexing` package under `ddl_metadata.meta_projection`, update active imports and test paths, then remove the retired package without a compatibility shim; do not delete behavior tests in this mechanical slice. |  |  |
| TASK-004 | Run focused import, compile, Ruff, Pyright, and policy tests; verify no active code or test imports `data_agent.metadata_indexing`. |  |  |

### Implementation Phase 2

- **GOAL-002**: Replace snapshot persistence coupling with a deep accepted-snapshot publication seam while preserving atomicity.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Add failing public-seam tests for lock-through-commit, rollback-before-unlock, post-commit unlock failure, lock contention, and all-or-nothing cross-context persistence. |  |  |
| TASK-006 | Introduce immutable `AcceptedSnapshot` input and `AcceptedSnapshotPublisher` using the existing `data_agent.models.physical`, `data_agent.models.semantic`, and `data_agent.models.memory` contract types; update workflow dependencies and nodes to call `publish` without importing ORM rows, external DTOs, or persistence implementations. |  |  |
| TASK-007 | Implement the MySQL accepted-snapshot integration adapter using one transaction and the existing generation locks, preserving previous-scope handling, Meta/Memory/Data Sync writes, desired-state calculation, and outbox enqueue. |  |  |
| TASK-008 | Move adapter construction to the DDL worker composition root, remove `MetadataSnapshotService` construction from lifecycle code, and delete replaced repository-call-order tests. |  |  |
| TASK-009 | Run focused unit and MySQL integration tests; use a controlled failing write to prove complete rollback and then restore the implementation. |  |  |

### Implementation Phase 3

- **GOAL-003**: Deepen Meta Projection dispatch, refresh, rebuild, and search modules around useful ports.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Add failing dispatcher seam tests for claim-remote-settle, cancellation propagation, local-progress defer, authority loss, retry/dead-letter, and one bounded value-refresh transition. |  |  |
| TASK-011 | Define `ProjectionWorkStore`, `ProjectionReader`, `SemanticIndex`, `ValueIndex`, and `ValueRefreshStore` contracts; refactor dispatcher and value refresh use cases to receive them and scalar configuration through constructors. |  |  |
| TASK-012 | Split the existing value-refresh file into deterministic policy, application state-machine, and MySQL persistence responsibilities while preserving scan/select/publish/cleanup cursors and byte budgets. |  |  |
| TASK-013 | Refactor rebuild and search use cases to injected ports, preserving destructive-rebuild recovery and authoritative Meta readback with visible-generation completeness. |  |  |
| TASK-014 | Move ES/Qdrant/TEI/MySQL implementations to adapters and construct them at startup; maintenance jobs invoke composed use cases instead of creating concrete clients or reading global settings. |  |  |
| TASK-015 | In the same green slices that establish equivalent public coverage, remove direct `_synchronize`, `_scan`, `_select_top_n`, `_publish`, and `_cleanup` tests plus duplicate generation/cursor cases; retain only external adapter contracts and the minimum non-duplicate policy examples. |  |  |

### Implementation Phase 4

- **GOAL-004**: Publish the stable Data Sync integration seam and prove cross-context dependency direction.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-016 | Add failing tests for `ValueProjectionParticipant.apply` covering before/after frequency changes, pending-generation safe skip, source isolation, and refresh enqueue in the caller's transaction. |  |  |
| TASK-017 | Add neutral `MaterializedTableRef` and `MaterializedRowsChanged` commands plus the transaction-scoped participant interface; implement a non-singleton MySQL adapter bound by the outer transaction factory to the exact `AsyncSession` used by the current Data Sync transaction, without exposing that session in the application contract. |  |  |
| TASK-018 | Minimally update current Data Sync outer call sites for backfill, reset, and buffered binlog to invoke the new public participant, remove all retired package imports, document the interface for Child 3, and do not introduce or redesign Data Sync application ports. |  |  |
| TASK-019 | Add a MySQL failure-injection integration test proving DW/Data Sync changes, frequency deltas, and refresh enqueue share one transaction and roll back together; leave Data Sync port definition, use-case injection, and internal restructuring to the dependent child. |  |  |

### Implementation Phase 5

- **GOAL-005**: Complete verification, documentation convergence, and handoff to Data Sync.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-020 | Run focused unit suites, metadata mapping/vector contract tests, accepted-snapshot MySQL tests, resumable-refresh integration tests, and Data Sync compatibility tests including shared-session rollback identity; report unavailable services explicitly. |  |  |
| TASK-021 | Run repository gates: `uv lock --check`, Ruff, Pyright, compileall, non-integration pytest, package/import searches, AST cycle check, and `git diff --check`; read complete outputs. |  |  |
| TASK-022 | Review every acceptance criterion, ensure the retired test/package paths are gone, and update applicable Trellis specs only for conventions proven by the implementation. |  |  |
| TASK-023 | Record the accepted-snapshot atomic integration-adapter decision as an ADR after verification, then provide the reviewed projection input contract to Child 3 without starting that task automatically. |  |  |

## 3. Alternatives

- **ALT-001**: Keep `metadata_indexing` as an independent bounded context. Rejected because it owns no authoritative business state and represents accepted DDL Metadata.
- **ALT-002**: Let the workflow depend on repositories through a broad UnitOfWork protocol. Rejected because it mirrors implementation detail, creates a shallow interface, and still exposes cross-context persistence choreography.
- **ALT-003**: Publish separate events after committing each context. Rejected because it changes the required all-or-nothing accepted-snapshot visibility contract.
- **ALT-004**: Pass `AsyncSession` through the Data Sync/Meta Projection application interface. Rejected because it leaks infrastructure into inner layers; the concrete transaction-scoped adapter captures the shared session instead.
- **ALT-005**: Preserve old imports with compatibility modules. Rejected because this is an internal hard migration and shims would hide ownership drift.

## 4. Dependencies

- **DEP-001**: The parent task's `CONTEXT.md`, `CONTEXT-MAP.md`, architecture review, request-flow research, sync-index research, and approved test-seam map are authoritative planning inputs.
- **DEP-002**: MySQL-backed accepted snapshot and resumable refresh verification requires the configured local MySQL service; Elasticsearch, Qdrant, TEI, and Redis availability must be reported independently.
- **DEP-003**: Child `08-02-data-sync-ports` depends on the reviewed `MaterializedRowsChanged` / `ValueProjectionParticipant` semantics from Phase 4.
- **DEP-004**: Existing database and external-service guidelines define lock duration, claim/remote/settle, database clock, retry, mapping, and resumability contracts.

## 5. Files

- **FILE-001**: `src/data_agent/ddl_metadata/application/accepted_snapshot.py` — accepted snapshot command and publication port.
- **FILE-002**: `src/data_agent/ddl_metadata/adapters/mysql/accepted_snapshot.py` — generation-lock and one-transaction integration adapter.
- **FILE-003**: `src/data_agent/ddl_metadata/workflow/contracts.py`, workflow nodes, and worker lifecycle — publisher dependency and composition updates.
- **FILE-004**: `src/data_agent/ddl_metadata/meta_projection/domain.py` — projection contracts and deterministic policies.
- **FILE-005**: `src/data_agent/ddl_metadata/meta_projection/application/` — dispatcher, value refresh, search, rebuild, and ports.
- **FILE-006**: `src/data_agent/ddl_metadata/meta_projection/adapters/` — MySQL, Elasticsearch, Qdrant/TEI, and composition implementations.
- **FILE-007**: `src/data_agent/metadata_indexing/` — retired after hard migration.
- **FILE-008**: `src/data_agent/data_sync/backfill.py` and composition code — minimal public-input call-site migration only; Data Sync ports and application refactor belong to the dependent child.
- **FILE-009**: `tests/unit/ddl_metadata/meta_projection/` and `tests/integration/ddl_metadata/` — new public seam and adapter contract suites.
- **FILE-010**: `tests/unit/metadata_indexing/` and old snapshot collaborator tests — moved mechanically first, then individual implementation-coupled cases removed only in the same green slice as replacement coverage.
- **FILE-011**: `.trellis/spec/backend/`, `CONTEXT.md`, `CONTEXT-MAP.md`, and ADR records — update only when implementation proves a durable convention or decision.

## 6. Testing

- **TEST-001**: Accepted snapshot unit seam: lock/commit/rollback/contention outcomes through `AcceptedSnapshotPublisher.publish`.
- **TEST-002**: Accepted snapshot MySQL integration: Meta, Memory, Data Sync desired, and projection outbox commit or rollback together.
- **TEST-003**: Dispatcher unit seam: bounded claim-remote-settle, cancellation, authority, retry/defer/dead-letter, and value phase advancement.
- **TEST-004**: Search unit seam: bounded semantic/value recall, authoritative readback, stale fingerprint rejection, and refresh-generation completeness.
- **TEST-005**: Value participant unit/integration seam: source-scoped before/after frequency deltas and refresh enqueue in the same transaction.
- **TEST-006**: Adapter contracts: MySQL lease/cursor semantics, ES mapping/analyzer/bulk budgets, Qdrant dimension/payload index, and TEI timeout/error propagation.
- **TEST-007**: Resumable refresh integration: scan/select/publish/cleanup restart and destructive-rebuild recovery.
- **TEST-008**: Structural verification: forbidden-import checks, no retired package imports, AST cycle scan, Ruff, Pyright, compileall, non-integration pytest, and diff checks.
- **TEST-009**: TDD evidence: for each new seam, observe the regression test fail before implementation or by reverting the focused fix, then restore and rerun green.

## 7. Risks & Assumptions

- **RISK-001**: Moving snapshot choreography can silently shorten the generation-lock window or split commits; transaction-level failure injection is mandatory.
- **RISK-002**: Splitting the value-refresh state machine can change cursor identity, byte budgets, or pending-generation promotion; preserve persisted state semantics and verify restart paths.
- **RISK-003**: A transaction-scoped participant may be wired with a different session than Data Sync; composition and integration tests must prove shared atomicity.
- **RISK-004**: Child 2 and Child 3 both touch the Data Sync bridge; restrict this task to the outer bridge and rebase/review overlap before Child 3 starts.
- **RISK-005**: Removing implementation-coupled tests too early can reduce protection; deletion occurs only in the same green slice that introduces equivalent public coverage.
- **ASSUMPTION-001**: No legacy database, vector index, or historical data requires migration.
- **ASSUMPTION-002**: Existing schemas, index names, external contracts, config keys, and log events are stable compatibility requirements.
- **ASSUMPTION-003**: The single MySQL engine/session can continue spanning the participating schemas as specified by current database guidelines.

## 8. Related Specifications / Further Reading

- `AGENTS.md`
- `CONTEXT.md`
- `CONTEXT-MAP.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/directory-structure.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/external-service-integrations.md`
- `.trellis/spec/backend/error-handling.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/guides/cross-layer-thinking-guide.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/architecture-combination-review.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/ddl-request-flow.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/sync-index-flow.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/test-seam-map.md`
