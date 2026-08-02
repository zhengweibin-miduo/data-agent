---
goal: Refactor Data Sync behind application ports and replace implementation-coupled service tests
version: 1.0
date_created: 2026-08-02
last_updated: 2026-08-02
owner: zwb
status: Checked
tags: [architecture, data-sync, ports-and-adapters, testing]
---

# Introduction

This plan implements the reviewed Data Sync application seam in vertical
red-green slices. It preserves the existing MySQL transaction and CDC contracts
and does not add a schema or data migration.

## 1. Requirements and constraints

- **REQ-001**: `DataSyncService.dispatch_once()` remains the sole application
  interface and claims at most one task per call.
- **REQ-002**: Data Sync application code depends only on domain/application
  values and abstract ports; it does not import SQLAlchemy, `MySQLDatabase`,
  concrete repositories, source clients, schema synchronizers, or projection
  adapters.
- **REQ-003**: Production composition provides task, source, materialization,
  lease-clock, schema, and Meta Projection input adapters.
- **REQ-004**: Data Sync imports no Meta Projection implementation; the worker
  composition root selects the reviewed transaction participant.
- **REQ-005**: Preserve phase, coordinate, cursor, readiness, retry/backoff,
  lease, generation/schema lock, ownership, and atomic projection invariants.
- **REQ-006**: Replace service tests that call private lifecycle helpers with
  tests through `dispatch_once()` or explicit external adapter contracts.
- **CON-001**: No database, bootstrap, vector-index, history, or data migration.
- **CON-002**: No frontend, HTTP/SSE, configuration-key, worker-command, or
  external payload change.
- **PAT-001**: Use module, interface, seam, adapter, depth, leverage, and locality
  vocabulary in design and review.

## 2. Implementation steps

### Phase 1: Application contracts and tracer task claim

- [x] Add side-effect-free `data_sync/application` and `data_sync/adapters`
  packages.
- [x] Define `SyncTaskPort`, `SourcePort`, `MaterializationPort`, and
  `LeaseCoordinator` without technology-specific types.
- [x] Add an in-memory seam test proving one `dispatch_once()` claims at most one
  task; run it red before implementing the new service constructor and claim
  path.
- [x] Move service orchestration to `data_sync/application/service.py`; delete
  the retired root service module and update imports without a compatibility
  shim.

### Phase 2: Phase orchestration slices

- [x] Add one red-green `dispatch_once()` slice for capture plus
  streaming-to-replaying behavior, then implement it through ports.
- [x] Add one red-green slice for saturated backfill drain plus cursor
  continuation, then implement it through ports.
- [x] Add one red-green slice for durable backfill throttling without dispatcher
  sleep, then implement it through ports.
- [x] Add one red-green slice for schema/generation contention rescheduling
  without retry consumption, then implement failure classification through the
  task port.
- [x] Add slices for deterministic conflict/pause, transient retry/dead state,
  unknown source, and lease loss using only observable durable adapter state.

### Phase 3: Production adapters and composition

- [x] Implement MySQL task operations as short caller-independent transactions
  around `DataSyncRepository`.
- [x] Implement the source adapter without exposing `AsyncEngine` to the
  application.
- [x] Implement materialization operations that preserve existing DDL,
  generation reset, backfill, replay, ownership, coordinate, and settlement
  transaction scopes.
- [x] Implement lease-clock coordination with periodic database-clock renewal
  and cancellation cleanup.
- [x] Make the low-level materialization functions require an injected neutral
  projection participant; remove their concrete Meta Projection adapter import.
- [x] Add composition that receives a projection-participant factory rather than
  reading global settings.
- [x] Update `worker.py` to select the MySQL/source/schema/projection adapters and
  retain startup probes and reverse-order shutdown.

### Phase 4: Replace tests and prove structure

- [x] Delete private lifecycle/call-order tests after equivalent behavior is
  covered through `dispatch_once()`.
- [x] Update the explicit MySQL materialization adapter tests to inject the
  projection participant directly.
- [x] Add a package-boundary test proving application modules do not import
  concrete infrastructure/adapters and Data Sync contains no retired
  `metadata_indexing` or concrete Meta Projection adapter import.
- [x] Run focused unit tests after every vertical slice.

### Phase 5: Full check and finish

- [x] Run all Data Sync unit tests.
- [x] Run non-integration pytest, Ruff, full Pyright, compileall, settings load,
  lock check, package build, and `git diff --check`.
- [x] Run live Data Sync/answer-readiness/Meta Projection integration scenarios
  only when the required local services are available; record unavailable
  services and real failures without destructive reprovisioning.
- [x] Search for forbidden imports and retired service paths.
- [x] Review and update backend directory/database/error/quality specs if the
  implementation establishes a durable convention.
- [x] Verify every PRD acceptance criterion against current code and fresh
  command output before committing.
- [ ] Create one coherent local work commit using the user's existing commit
  authorization; do not push.
- [ ] Archive the child task and record the Trellis session after the work commit
  leaves a clean task scope.

## 3. Validation commands

```powershell
uv run pytest tests/unit/data_sync
uv run pytest -m "not integration"
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv lock --check
uv build
git diff --check
```

Live checks, when services are available:

```powershell
uv run pytest tests/integration/data_sync
uv run pytest tests/integration/answer_readiness
uv run pytest tests/integration/ddl_metadata/test_meta_projection_resumable_refresh.py
```

## 4. Review gates

- The application module contains no SQLAlchemy, global settings,
  infrastructure, concrete repository/source/schema, or adapter imports.
- The production adapter keeps generation lock through DDL settlement and
  managed Session commit.
- DW DML, ownership, event/cursor/coordinate state, and value projection input
  remain atomic.
- Tests do not call or monkeypatch private application lifecycle methods.
- No schema/bootstrap/migration file changes.
- No compatibility shim remains at `data_sync.service`.

## 5. Rollback points

- After Phase 1, remove the new packages and restore the original service import
  before any production composition change.
- After Phase 3, revert the child commit as one unit; do not preserve parallel
  old/new service paths.
- On transaction or lock-contract regression, stop and revert rather than
  weaken the invariant or add a migration.
