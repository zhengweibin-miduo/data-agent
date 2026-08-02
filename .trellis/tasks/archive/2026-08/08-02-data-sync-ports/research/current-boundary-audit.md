# Data Sync current boundary audit

## Scope

This audit records the current implementation evidence used to design the Data
Sync application ports after the reviewed Meta Projection interface landed in
commit `3ceea69`.

## Current application coupling

- `src/data_agent/data_sync/service.py:13-32` imports concrete backfill
  functions, `MySQLSourceClient`, `DataSyncRepository`, `DWSchemaSynchronizer`,
  `MySQLDatabase`, and concrete lock errors.
- `DataSyncService.dispatch_once()` opens a managed MySQL Session and constructs
  `DataSyncRepository` directly (`service.py:53-63`).
- `_process()` opens concrete Sessions throughout phase orchestration and calls
  concrete source, repository, schema, and materialization implementations
  (`service.py:105-235`).
- `_synchronize_schema()` owns the generation lock, DDL/provenance Sessions,
  synchronizer construction, and settlement (`service.py:261-299`).
- `_capture()`, `_retry()`, `_renew_lease()`, `_hold()`, and `_reschedule()` all
  construct concrete persistence dependencies (`service.py:300-410`).

## Projection input coupling

- The reviewed neutral input interface is
  `ddl_metadata/meta_projection/application/value_input.py`:
  `ValueProjectionParticipant.prepare()` returns a transaction-bound
  `PreparedValueProjection`, whose `apply()` receives before/after row changes.
- `data_sync/backfill.py:27-34` currently imports both that application
  interface and the concrete `MySQLValueProjectionParticipant` adapter.
- Backfill, replay, and generation reset correctly update DW rows, ownership,
  task coordinates/cursors, and Meta Projection frequency state in one caller
  Session (`backfill.py:74-125`, `158-224`, `227-319`).
- The concrete projection participant must therefore be selected by the worker
  composition root and injected into the MySQL materialization adapter; moving
  it behind an independent transaction would violate the existing atomicity
  contract.

## Existing test coupling

- `tests/unit/data_sync/test_service.py` calls or replaces `_process`,
  `_capture`, `_retry`, `_reschedule`, `_renew_lease`,
  `_with_lease_heartbeat`, and `_synchronize_schema` directly.
- The same tests monkeypatch `MySQLDatabase`, `DataSyncRepository`, concrete
  materialization functions, and the schema synchronizer. They describe the
  current implementation graph rather than the public `dispatch_once()` seam.
- `tests/unit/data_sync/test_backfill.py` is an explicit MySQL materialization
  adapter contract. It may retain SQL-oriented assertions where they prove
  atomic ownership, row mutation, coordinate/cursor, and projection input
  behavior, but it must inject the neutral projection participant explicitly.

## Design consequences

1. Keep `DataSyncService.dispatch_once()` as the sole application interface.
2. Move orchestration under `data_sync/application/`; it must not import
   SQLAlchemy, `MySQLDatabase`, concrete repositories, source clients, schema
   synchronizers, or projection adapters.
3. Define driven ports for durable task operations, named source reads,
   transactional DW materialization/schema work, and lease-clock coordination.
4. Build MySQL adapters around the existing repository, backfill, schema, lock,
   and source implementations so their proven transaction behavior remains
   local.
5. Make the projection participant mandatory at the low-level materialization
   calls. The worker composition root supplies
   `MySQLValueProjectionParticipant`; Data Sync no longer imports that concrete
   DDL adapter.
6. Replace private-method service tests with in-memory adapters exercised only
   through `dispatch_once()` and preserve live CDC/DW integration scenarios.

## Risks to verify

- Generation lock must remain held through DDL Session commit and phase
  settlement.
- Saturated backfill must apply each event in its own transaction and renew the
  lease between events.
- Captured event persistence and the streaming-to-replaying phase change must
  remain atomic.
- DW DML, key ownership, event acknowledgement, applied coordinate, and Meta
  Projection value input must remain in one transaction.
- Lease cancellation must await operation cleanup before returning.
- No database schema or historical-data migration is permitted.
