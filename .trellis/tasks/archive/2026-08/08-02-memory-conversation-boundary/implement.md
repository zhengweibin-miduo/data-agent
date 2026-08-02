---
goal: Refactor Conversation and Long-term Memory around application ports while preserving authority, atomicity, and projection convergence
version: 1.0
date_created: 2026-08-02
last_updated: 2026-08-02
owner: zwb
status: Planned
tags: [architecture, ddd, memory, conversation, tdd]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan replaces concrete cross-context and infrastructure dependencies with deep application seams. It preserves the two intentional cross-context MySQL transactions through explicit outer integration adapters and replaces implementation-coupled tests vertically.

## 1. Requirements & Constraints

- **REQ-001**: Conversation application modules depend on explicit stores and Long-term Memory interfaces, never `memory.mysql` or infrastructure clients.
- **REQ-002**: Memory application service, search, and projection dispatch modules depend on domain values and ports, never concrete repositories, SDKs, global settings, or session creation.
- **REQ-003**: User-data erasure atomically deletes Conversation authority/outbox and tombstones all user Memory authority/outbox.
- **REQ-004**: Extraction completion atomically upserts validated Memory candidates and finishes Conversation summary/outbox after model work outside the transaction.
- **REQ-005**: Preserve turn idempotency, active-turn gate, tenant isolation, authoritative MySQL history/lifecycle, tombstone-before-purge, and rebuildable projection convergence.
- **CON-001**: Preserve HTTP, Redis, MySQL, index, configuration, logging, and package contracts.
- **CON-002**: Do not add database, vector-index, or history migration, dual-write, or compatibility paths.
- **GUD-001**: Use red-green-replace through public seams; delete replaced collaborator/private tests in the same green slice.
- **PAT-001**: Do not mirror repository methods into shallow ports; use use-case-level interfaces and explicit integration adapters.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Establish enforceable application boundaries and production composition.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-001 | Add failing AST/package tests forbidding Conversation application imports of `memory.mysql`/infrastructure and Memory application imports of concrete adapters, SQLAlchemy, settings, or SDKs. |  |  |
| TASK-002 | Define Conversation store, Memory reader, user-data eraser, extraction committer, Memory store/search/index ports, and injected configuration values. |  |  |
| TASK-003 | Add MySQL and external-index adapters plus composition factories without creating empty layers or repository-shaped protocols. |  |  |

### Implementation Phase 2

- **GOAL-002**: Refactor Conversation use cases while preserving lifecycle and atomic cross-context behavior.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-004 | Add failing public-seam tests for create/list/history/start/complete/delete, recall, tenant isolation, and idempotent turn behavior. |  |  |
| TASK-005 | Inject `ConversationStore` and `LongTermMemoryReader` into Conversation service; move MySQL transaction construction to adapters. |  |  |
| TASK-006 | Implement `UserDataEraser` as one MySQL integration adapter and prove full rollback on either context failure. |  |  |
| TASK-007 | Replace concrete Memory constructor/repository assertions after equivalent Conversation seam coverage is green. |  |  |

### Implementation Phase 3

- **GOAL-003**: Refactor extraction collaboration without splitting its atomic commit.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-008 | Add failing extraction seam tests for claim, model-outside-transaction, evidence validation, atomic commit, lease release, and retry. |  |  |
| TASK-009 | Inject Conversation claim store and `ExtractionCommitter`; implement the MySQL adapter that combines Memory upsert with Conversation finish in one transaction. |  |  |
| TASK-010 | Retain evidence/quote/role/order tests and remove tests that only assert concrete repository construction or internal call order. |  |  |

### Implementation Phase 4

- **GOAL-004**: Refactor Memory service and search around authoritative and derived-signal ports.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-011 | Add failing Memory service/search seam tests for authority, history, update/delete, exact fallback, remote degradation, pending signal filtering, guards, and access-stat failure. |  |  |
| TASK-012 | Inject Memory store, search store, lexical/vector indexes, embedding provider, budgets, versions, and timeout values; remove application-layer concrete construction. |  |  |
| TASK-013 | Implement MySQL/search-index adapters and wire API/Chat composition while preserving public response contracts. |  |  |
| TASK-014 | Replace module-global patches and duplicate collaborator tests after public seam coverage is green. |  |  |

### Implementation Phase 5

- **GOAL-005**: Refactor Memory projection dispatch and worker maintenance.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-015 | Add failing dispatcher seam tests for claim-remote-settle, non-active delete, authority loss, per-target failure, durable convergence, and dead letters. |  |  |
| TASK-016 | Move dispatcher orchestration behind injected work/index ports and construct one long-lived runtime at worker startup. |  |  |
| TASK-017 | Inject Memory expire/purge maintenance use cases; remove cron-time dispatcher/repository construction. |  |  |
| TASK-018 | Delete replaced private dispatcher/collaborator tests while retaining repository, mapping, lease, and external adapter contracts. |  |  |

### Implementation Phase 6

- **GOAL-006**: Verify compatibility, update durable specifications, and finish the task.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-019 | Run focused red-green checks, Ruff, Pyright, compileall, non-integration pytest, relevant MySQL/Redis/index integration tests, import checks, and package build. |  |  |
| TASK-020 | Reconcile PRD acceptance, update only implementation-proven specs/context decisions, and record unavailable external services or schema drift honestly. |  |  |
| TASK-021 | Run Trellis check, fix verified findings, record verification evidence, and complete Trellis lifecycle without performing unauthorized Git push or database migration. |  |  |

## 3. Alternatives

- **ALT-001**: Pass `AsyncSession` through application ports. Rejected because it leaks infrastructure and makes cross-context coupling implicit.
- **ALT-002**: Split user deletion and extraction completion into asynchronous events. Rejected because it changes existing all-or-nothing authority contracts.
- **ALT-003**: Wrap every repository method in a Protocol. Rejected because it creates shallow interfaces with no locality or leverage.
- **ALT-004**: Keep module-global clients and patch them in tests. Rejected because application behavior remains coupled to adapter construction and tests stay implementation-oriented.

## 4. Dependencies

- **DEP-001**: Parent architecture review, import graph, Memory/Conversation request flow, and approved test seam map.
- **DEP-002**: Backend directory, database, external-service, error, and quality specifications.
- **DEP-003**: MySQL/Redis/Elasticsearch/Qdrant/TEI availability for integration verification; unavailable services are reported, not treated as passing.

## 5. Files

- **FILE-001**: `src/data_agent/conversation/application/` and `conversation/adapters/mysql/` — Conversation ports, use cases, and integration adapters.
- **FILE-002**: `src/data_agent/memory/application/` and `memory/adapters/` — Memory services, search, dispatcher ports, and adapters.
- **FILE-003**: `src/data_agent/application.py` and `ddl_metadata/worker/` — HTTP and worker composition roots.
- **FILE-004**: `tests/unit/conversation/`, `tests/unit/memory/`, and relevant integration suites — public seams and retained adapter contracts.
- **FILE-005**: `.trellis/spec/backend/`, `CONTEXT-MAP.md`, and task verification artifacts — update only for proven conventions.

## 6. Testing

- **TEST-001**: Conversation public seam and atomic user-data erase.
- **TEST-002**: Extraction validation, model/transaction separation, atomic Memory+Conversation commit, and retry.
- **TEST-003**: Memory service/search authority and independently degrading derived signals.
- **TEST-004**: Memory dispatcher claim-remote-settle and convergence.
- **TEST-005**: MySQL repository/transaction, Redis turn/outbox, ES/Qdrant mapping, and lease adapter contracts.
- **TEST-006**: Static forbidden imports, Ruff, Pyright, compileall, non-integration pytest, focused integrations, and package build.

## 7. Risks & Assumptions

- **RISK-001**: Moving transaction boundaries can split Conversation/Memory atomic writes; failure-injection integration tests are mandatory.
- **RISK-002**: Search refactoring can accidentally trust stale ES/Qdrant content or lose exact fallback; authoritative readback tests are mandatory.
- **RISK-003**: Dispatcher refactoring can hold transactions across remote calls or acknowledge stale work; transaction-order and authority tests are mandatory.
- **RISK-004**: Removing patched collaborator tests before seam coverage can hide behavior loss; deletion must occur in the same green slice.
- **ASSUMPTION-001**: No legacy database, index, or history migration is required or authorized.
- **ASSUMPTION-002**: Existing external contracts and persisted schemas remain authoritative compatibility constraints.

## 8. Related Specifications / Further Reading

- `AGENTS.md`
- `CONTEXT.md`
- `CONTEXT-MAP.md`
- `.trellis/spec/backend/directory-structure.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/external-service-integrations.md`
- `.trellis/spec/backend/error-handling.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/guides/cross-layer-thinking-guide.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/import-graph-audit.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/memory-conversation-flow.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/test-seam-map.md`
