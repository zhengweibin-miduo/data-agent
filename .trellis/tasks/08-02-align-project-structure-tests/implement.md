---
goal: Coordinate four vertical refactors that align project structure, DDD boundaries, frontend modules, and tests
version: 1.0
date_created: 2026-08-02
last_updated: 2026-08-02
owner: zwb
status: Planned
tags: [architecture, refactor, ddd, frontend, testing]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This parent plan coordinates four independently verifiable child tasks. The parent owns requirements, context relationships, dependency ordering, and final integration; it does not implement business code.

## 1. Requirements & Constraints

- **REQ-001**: Preserve the independent `frontend/` and API-only `src/data_agent/` ownership model.
- **REQ-002**: Make changed backend application modules depend on domain models and abstract ports instead of concrete infrastructure or persistence implementations.
- **REQ-003**: Treat Meta Projection as a rebuildable capability owned by DDL Metadata; Meta Snapshot remains authoritative.
- **REQ-004**: Remove the Data Sync and Meta Projection bidirectional implementation dependency.
- **REQ-005**: Keep tests only at the six user-approved public seams or explicit external-adapter contracts.
- **CON-001**: Do not add database, vector-index, or historical-data migration paths without explicit user authorization.
- **CON-002**: Preserve current external HTTP/SSE, Redis, MySQL, LangGraph, configuration and legacy-frontend contracts unless a child PRD explicitly changes one.
- **CON-003**: Parent task metadata must be committed before child worktrees are created so parent/child links are valid.
- **GUD-001**: Use vertical red-green slices and replace old implementation-coupled tests after the new interface proves the same behavior.
- **PAT-001**: Use module, interface, seam, adapter, depth, leverage and locality terminology in child designs and reviews.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Create the Trellis child-task tree from a committed parent planning baseline.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Validate parent `prd.md`, `design.md`, `implement.md`, context documents and research artifacts; commit only the parent planning baseline. | ✅ | 2026-08-02 |
| TASK-002 | Create `refactor-memory-conversation-boundary` with its own branch/worktree and parent link. | ✅ | 2026-08-02 |
| TASK-003 | Create `refactor-meta-projection-boundary` with its own branch/worktree and parent link. | ✅ | 2026-08-02 |
| TASK-004 | Create `refactor-data-sync-ports` with its own branch/worktree and parent link; record dependency on the reviewed Child 2 projection interface. | ✅ | 2026-08-02 |
| TASK-005 | Create `refactor-workbench-modules` with its own branch/worktree and parent link. | ✅ | 2026-08-02 |

### Implementation Phase 2

- **GOAL-002**: Plan and execute each vertical child through Trellis without starting the parent as an implementation target.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-006 | Complete Child 1 PRD/design/implement/context manifests, obtain review, activate, implement and check Memory/Conversation ports and tests. |  |  |
| TASK-007 | Complete Child 2 planning, obtain review, activate, implement and check accepted-snapshot/Meta-Projection ownership, ports and tests. |  |  |
| TASK-008 | After Child 2 exposes the reviewed projection interface, complete Child 3 planning and implement/check Data Sync ports and structural-cycle removal. |  |  |
| TASK-009 | Complete Child 4 planning, obtain review, activate, implement and check Workbench internal modules and tests. |  |  |

### Implementation Phase 3

- **GOAL-003**: Integrate child outcomes and prove repository-wide requirements.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Reconcile context/spec updates and verify no child introduced overlapping or contradictory ownership rules. |  |  |
| TASK-011 | Run full backend, frontend, packaging, configuration and diff verification gates and read complete outputs. |  |  |
| TASK-012 | Verify each PRD acceptance criterion and the six approved test seams, then archive children and parent according to Trellis workflow. |  |  |

## 3. Alternatives

- **ALT-001**: Implement everything on one branch. Rejected because the change spans independent bounded contexts, frontend state orchestration and large test suites, producing an unsafe review and rollback surface.
- **ALT-002**: Create separate backend-test and frontend-test tasks. Rejected because horizontal test work would violate vertical TDD slices and would likely layer new tests on top of old implementation-coupled tests.
- **ALT-003**: Treat Meta Projection as an independent bounded context. Rejected by user decision because it has no independent authoritative state or ubiquitous language and primarily represents accepted DDL Metadata.

## 4. Dependencies

- **DEP-001**: Child 3 depends on Child 2's reviewed Meta Projection input interface; the dependency is explicit and is not implied solely by the parent/child tree.
- **DEP-002**: All children depend on the committed parent context model, six approved test seams and compatibility constraints.
- **DEP-003**: Full integration verification depends on locally available MySQL/Redis services; unavailable optional services must be reported rather than treated as passing.

## 5. Files

- **FILE-001**: `.trellis/tasks/08-02-align-project-structure-tests/` — parent requirements, design, implementation plan and research.
- **FILE-002**: `CONTEXT.md` and `CONTEXT-MAP.md` — canonical domain terms and bounded-context relationships.
- **FILE-003**: `.trellis/tasks/<child>/` — each child task's PRD, design, implementation plan, research and context manifests.
- **FILE-004**: `.trellis/spec/backend/` and `.trellis/spec/frontend/` — updated only when child implementation proves new executable conventions.

## 6. Testing

- **TEST-001**: Child-focused red-green tests at the approved seam; revert the corresponding fix to prove each new regression test fails, then restore and rerun.
- **TEST-002**: Backend gates: `uv lock --check`, Ruff, Pyright, compileall, settings loading and non-integration pytest.
- **TEST-003**: Relevant MySQL/Redis/CDC/index integration tests with service availability stated explicitly.
- **TEST-004**: Frontend gates from `frontend/`: `npm ci`, lint, typecheck, test and build.
- **TEST-005**: Parent integration: full requirement checklist, cross-context import/path searches, package build/legacy asset verification and `git diff --check`.

## 7. Risks & Assumptions

- **RISK-001**: Child 2 and Child 3 can overlap in Data Sync/Meta Projection integration files; enforce the declared dependency and re-review scope before implementation.
- **RISK-002**: Replacing private-helper tests before a public interface covers the behavior can silently reduce regression protection.
- **RISK-003**: Moving a transaction seam can break atomic Meta/Memory/Data Sync/outbox publication even when unit tests pass; retain transaction-level integration evidence.
- **RISK-004**: Handwritten frontend contract projections can drift if a backend contract changes; update backend authority first and verify both sides.
- **ASSUMPTION-001**: No legacy database, vector index or historical data requires migration.
- **ASSUMPTION-002**: Existing HTTP/SSE, Redis, MySQL, LangGraph and configuration contracts remain stable unless a child PRD explicitly changes them.

## 8. Related Specifications / Further Reading

- `AGENTS.md`
- `CONTEXT.md`
- `CONTEXT-MAP.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/architecture-combination-review.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/test-seam-map.md`
