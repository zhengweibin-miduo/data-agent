# Implementation Plan

## 1. Preserve JSON SQL NULL at the Binlog Boundary

- [x] Add an instance-local `mysql-replication==1.0.16` row decoder adapter in
  `src/data_agent/data_sync/binlog.py`.
- [x] Bind the adapter to the exact
  `_RowsEvent__read_values_name(column, null_bitmap, null_bitmap_index,
  is_partial, cols_bitmap, unsigned, i)` instance attribute and delegate the
  complete original signature for every non-SQL-NULL-JSON case.
- [x] Introduce a private SQL-NULL sentinel used only before canonical event
  encoding.
- [x] Preserve JSON literal `null`, non-null JSON, non-JSON nulls and all three
  ROW operation shapes.
- [x] Add focused dependency-signature, lazy first-row decode, INSERT, UPDATE
  before/after, DELETE and codec regressions to
  `tests/unit/data_sync/test_binlog.py`; retain the FULL-row-image boundary.
- [x] Extend the live CDC pipeline with distinct SQL `NULL` and JSON literal
  `null` source values and DW assertions.

Validation:

```powershell
uv run pytest tests/unit/data_sync/test_binlog.py
uv run pytest tests/integration/data_sync/test_cdc_pipeline.py -k json
```

Rollback point: this step changes no durable schema. Revert the adapter and its
tests if the dependency boundary cannot be verified.

## 2. Add Shared Advisory-Lock Infrastructure

- [x] Add typed, bounded multi-lock ownership to
  `src/data_agent/infrastructure/mysql.py`.
- [x] Add deterministic hashed generation lock naming in the `data_sync`
  package.
- [x] Acquire locks in byte-stable order and release in reverse order.
- [x] Ensure timeout, partial acquisition, business exception, cancellation and
  release-failure paths cannot return a lock-owning connection to the pool.
- [x] Add focused MySQL lifecycle unit/integration tests.

Validation:

```powershell
uv run pytest tests/unit/infrastructure/test_mysql.py
uv run pytest tests/integration/infrastructure/test_mysql.py
```

Rollback point: no caller uses the new context until Steps 3 and 4.

## 3. Serialize Accepted Generation Publication

- [x] Compute unique target generation locks before the accepted-snapshot
  transaction.
- [x] Hold them outside the managed Session so commit/rollback completes before
  release.
- [x] Map contention to a bounded retryable safe error.
- [x] Preserve one atomic Meta + memory + outbox + desired-state commit.
- [x] Update persistence tests to prove commit ordering and rollback cleanup.

Validation:

```powershell
uv run pytest tests/integration/persistence
uv run pytest tests/integration/test_ddl_metadata_flow.py
```

Rollback point: publisher and worker lock adoption must land together; do not
commit or push a one-sided lock protocol.

## 4. Serialize Worker Authority Check and DDL

- [x] Acquire the same target generation lock before opening the DDL Session.
- [x] Re-check authority with `DataSyncRepository` on that Session before schema
  synchronization and before each DDL.
- [x] Replace the separate-session `_has_authority()` callback with a
  parameterless callback closed over the DDL Session's repository.
- [x] Keep the existing schema lock and enforce generation-before-schema order.
- [x] Remove the separate-session `_has_authority()` race.
- [x] Reschedule generation-lock contention without consuming retry attempts.
- [x] Add deterministic concurrency regressions for worker-first and
  publisher-first orderings.

Validation:

```powershell
uv run pytest tests/unit/data_sync/test_schema_sync.py tests/unit/data_sync/test_service.py
uv run pytest tests/integration/data_sync/test_schema_sync.py
```

Rollback point: if the shared lock cannot cover both participants on the same
MySQL server, return to planning; do not replace it with a local process lock.

## 5. Specs and Full Quality Gate

- [x] Update backend database and external-integration specs with JSON null and
  generation-lock contracts.
- [x] Run full-scope Trellis check against PRD, design and implementation plan.
- [x] Read and apply `code_review.md` before the final PR assessment.
- [x] Inspect the complete diff for credentials, row payload logging, unrelated
  formatting and lock-order drift.

Validation:

```powershell
uv sync --locked
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv run pytest -m "not integration"
uv run pytest tests/integration/data_sync
uv run pytest tests/integration/persistence
docker compose -f docs/docker/docker-compose.yml config
git diff --check
python ./.trellis/scripts/task.py validate 07-29-resolve-json-null-ddl-lock
```

## 6. Commit, Push and Review Closure

- [x] Confirm remote PR head still matches the task start point plus any
  explicitly expected task commits; stop on unexpected movement.
- [ ] Present the Trellis commit plan and obtain one-shot commit confirmation.
- [ ] Create the approved Chinese Conventional Commit(s) without amend.
- [ ] Ordinary-push the verified task HEAD to
  `origin/feature/llm-data-sync-status-tool-20260727`; never force-push.
- [ ] Re-read both thread states at the pushed SHA.
- [ ] Reply with the actual commit and verification summary, then resolve only
  `discussion_r3671097004` and `discussion_r3673800168`.
- [ ] Confirm PR `quality` and thread-resolution state after the push.

Do not reply “已修复” or resolve either thread unless implementation, required
validation and push all succeeded.
