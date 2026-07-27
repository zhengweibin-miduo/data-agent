# Implementation Plan

## 1. Contracts and Configuration

- [x] Add typed data-sync desired schema, phase, Binlog coordinate, row event, and conflict contracts.
- [x] Reject accepted physical tables without a declared primary key before Meta persistence.
- [x] Add validated `data_sync`, `dw`, named-source, Binlog, lease, retry, and backfill settings.
- [x] Update default configuration without logging or exposing source credentials.
- [x] Add focused model/settings tests, including duplicate source names/server IDs and schema-name collisions.

Validation:

```powershell
uv run pytest tests/unit/test_settings.py tests/unit/test_model_descriptions.py
uv run pytest tests/unit/ddl_metadata/test_validation.py
uv run python -m data_agent.settings
```

## 2. Bootstrap and Target Persistence

- [x] Add the `data_sync` bootstrap database and the minimal task, event, and key-owner tables with Chinese comments.
- [x] Add matching schema-qualified SQLAlchemy Core definitions.
- [x] Implement repositories for desired-state upsert, lease claim/renewal, bounded retry/dead-letter settlement, event append/read/ack, offset progression, backfill cursor, and target-key ownership.
- [x] Keep repository methods caller-transaction-owned and database-clock-based.
- [x] Add bootstrap parity and live repository integration tests.

Validation:

```powershell
docker compose -f docs/docker/docker-compose.yml config
uv run pytest tests/unit -k "data_sync or bootstrap"
uv run pytest tests/integration -k "data_sync_repository"
```

## 3. Durable Meta-to-Sync Handoff

- [x] Derive one bounded desired-sync document from accepted physical/semantic/metric contracts.
- [x] Enqueue/upsert desired sync state in the existing accepted-snapshot target transaction.
- [x] Preserve DDL Job success semantics: no DW/source call in the graph; repeated snapshots remain idempotent.
- [x] Test rollback when desired-state persistence fails and idempotent replay when it succeeds.

Validation:

```powershell
uv run pytest tests/integration/persistence
uv run pytest tests/integration/test_ddl_metadata_flow.py
```

## 4. DW Schema Synchronization

- [x] Implement current-schema inspection and deterministic create/add/safe-widen planning.
- [x] Quote validated dynamic identifiers through the MySQL dialect.
- [x] Reject drop, rename, narrowing, ambiguous conversions, and missing metric dependency columns as non-retryable conflicts.
- [x] Re-introspect before every DDL retry because MySQL DDL auto-commits.
- [x] Add focused unit and live MySQL integration checks for no-op replay and partial-retry convergence.

Validation:

```powershell
uv run pytest tests/unit/data_sync/test_schema_sync.py
uv run pytest tests/integration/data_sync/test_schema_sync.py
```

## 5. Source and Binlog Adapter

- [x] Add the direct MySQL Binlog dependency and lock it with `uv`.
- [x] Add explicit per-source lifecycle, capability checks, timeouts, unique replication server IDs, and safe close behavior.
- [x] Decode ROW INSERT/UPDATE/DELETE events into one typed canonical payload.
- [x] Filter strictly to accepted `(source_schema, source_table)` tasks.
- [x] Persist event identity and payload idempotently without logging row values.
- [x] Add deterministic adapter tests and a live local Binlog capability check.

Validation:

```powershell
uv lock --check
uv run pytest tests/unit/data_sync/test_binlog.py
uv run pytest tests/integration/data_sync/test_binlog.py
```

## 6. Backfill, Replay, and Streaming

- [x] Implement simple/composite primary-key keyset chunking with configurable batch size and delay.
- [x] Persist progress only after target batch commit and resume from the last completed key.
- [x] Atomically apply target row DML, key ownership, event acknowledgement, and Binlog coordinate advancement across `dw` and `data_sync`.
- [x] Preserve key-owner tombstones after DELETE.
- [x] Stop on cross-source key conflict without overwriting DW or advancing the event.
- [x] Replay buffered events to the capture tail, switch to streaming, and clean acknowledged events in bounded batches.
- [x] Add crash/retry, large-table chunking, delete, conflict, and catch-up tests.

Validation:

```powershell
uv run pytest tests/unit/data_sync
uv run pytest tests/integration/data_sync
```

## 7. Dedicated CDC Process and Operations

- [x] Add a dedicated CDC process entrypoint and lifecycle; do not run the stream inside arq.
- [x] Apply the existing logging AOP boundary at process/task registration points.
- [x] Add dead-letter/conflict backlog reporting without a public API.
- [x] Update local Compose Binlog settings and replication-user bootstrap.
- [x] Verify shutdown releases source streams and target resources.

Validation:

```powershell
docker compose -f docs/docker/docker-compose.yml config
uv run pytest tests/unit/data_sync/test_worker.py
uv run pytest tests/integration/data_sync/test_worker.py
```

## 8. Full Quality and Review Gates

- [x] Update backend specs for the implemented database, external-service, error, and operational contracts.
- [x] Run Trellis check against PRD and design.
- [x] Read and follow root `code_review.md` for the final AI review.
- [x] Inspect the complete diff and confirm no credentials, row payloads, unrelated formatting, or destructive bootstrap operations.

Validation:

```powershell
uv sync --locked
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv run pytest -m "not tei"
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

Rollback points:

- Before Step 3, removal is configuration/schema-only.
- After Step 3, disable desired-state creation and stop the CDC process; retain `data_sync` rows for diagnosis.
- Never drop or truncate DW, Meta, application-memory, or developer Docker volumes as rollback.
