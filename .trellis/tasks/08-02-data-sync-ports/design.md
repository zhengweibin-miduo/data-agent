# Data Sync application ports design

## Purpose

Turn Data Sync into a deep application module whose callers and tests learn one
interface, `DataSyncService.dispatch_once()`, while MySQL sessions, repositories,
source engines, schema locks, DW SQL, and Meta Projection transaction
participation remain local to adapters.

## Module and seam

### External interface

`DataSyncService.dispatch_once() -> int` is the application interface and the
primary test seam. One call claims at most one durable task and executes one
bounded phase step. The result is the number of claimed tasks, not an internal
call trace.

The module is deep because this single interface hides phase dispatch,
captured/applied coordinate rules, bounded backfill/replay, readiness phase
changes, lease renewal, retry classification, generation/schema lock ordering,
and transactional projection input.

### Driven ports

The application module defines these interfaces:

1. **SyncTaskPort** — durable task and event-buffer operations required by phase
   orchestration: claim one task, read/record capture, read/clean events, settle
   a phase, retry, hold, reschedule, and renew a lease. Its interface uses only
   Data Sync domain/application values and never exposes `AsyncSession`.
2. **SourcePort** — named source capabilities required after startup: select
   access, current coordinate, bounded capture, and bounded keyset backfill.
   It does not expose a SQLAlchemy engine.
3. **MaterializationPort** — transaction-owning DW operations: synchronize
   schema, reset one generation unit, apply and settle one backfill batch, apply
   one saturated-buffer event, and apply/clean/settle one replay event. Each
   method hides the Sessions, repositories, SQL, lock ordering, and projection
   participant required to preserve atomicity.
4. **LeaseCoordinator** — runs a long operation with periodic database-clock
   lease renewal and supports an explicit renewal between bounded transactions.
   Its adapter owns monotonic waiting/cancellation details; callers never sleep
   to represent a durable deadline.

The ports are real seams: production uses MySQL/source adapters while service
tests use in-memory adapters. Adapter-contract and live integration tests cover
the production side.

## Adapter placement

- `data_sync/application/contracts.py` owns the four port interfaces and stable
  application result values.
- `data_sync/application/service.py` owns phase orchestration and failure
  classification. It imports only Data Sync values, settings values, safe
  business errors, and application ports.
- `data_sync/adapters/mysql.py` implements durable task operations,
  transactional materialization/schema work, and lease coordination using the
  existing `DataSyncRepository`, `MySQLDatabase`, backfill functions,
  `DWSchemaSynchronizer`, and generation locks.
- `data_sync/adapters/source.py` wraps `MySQLSourceClient` and the existing
  keyset backfill reader behind `SourcePort`.
- `data_sync/adapters/composition.py` assembles the application module without
  importing global settings.
- `data_sync/worker.py` remains the composition root. It creates concrete
  source clients and passes the reviewed
  `MySQLValueProjectionParticipant` factory into Data Sync composition.

Existing `repository.py`, `backfill.py`, `schema_sync.py`, and `binlog.py`
remain concrete implementation modules for this incremental migration. No
empty domain layer or compatibility shim is introduced.

## Meta Projection input

The stable cross-context input is the reviewed
`ValueProjectionParticipant.prepare(MaterializedTableRef)` interface owned by
DDL Metadata. Data Sync materialization functions require that participant
explicitly and depend only on the application interface. They no longer create
or import `MySQLValueProjectionParticipant`.

The worker composition root is the only place that selects the concrete
participant factory. The MySQL materialization adapter creates it with the same
caller-owned Session and `DesiredSyncTable`, preserving this order in one
transaction:

1. prepare and lock projection frequency state;
2. mutate DW rows and key ownership;
3. persist cursor/event/coordinate state;
4. apply before/after frequency changes and enqueue refresh desired state;
5. commit or roll back all changes together.

## Phase flow

### Claim and dispatch

1. `dispatch_once()` asks `SyncTaskPort` to claim at most one task.
2. It selects the named `SourcePort`; an unknown source is a deterministic
   business failure.
3. It delegates exactly one bounded phase step.
4. Failure classification settles only through `SyncTaskPort`; stale workers do
   not consume retry budget.

### Pending schema

The source adapter checks select access. `LeaseCoordinator` guards
`MaterializationPort.synchronize_schema()`. The MySQL adapter retains the global
order `generation lock -> schema lock -> task authority`, uses READ COMMITTED for
the authority Session and REPEATABLE READ for provenance, and settles
`BUFFERING` before releasing the generation lock.

### Buffering and generation reset

The source returns the current Binlog coordinate. A guarded materialization
operation removes one bounded unit of the old generation. If more rows remain,
the adapter reschedules `BUFFERING`; otherwise it atomically records snapshot
and captured coordinates and enters `BACKFILLING`.

### Backfill

The application first captures up to the durable event-buffer budget. When the
buffer is saturated, it reads a bounded event page, explicitly renews the lease
between events, and applies each event in its own transaction before bounded
cleanup. It then obtains one guarded source keyset batch. A non-empty batch is
written and settled back to `BACKFILLING` with a database-clock delay in one
transaction; an empty batch enters `REPLAYING`.

### Replay and streaming

The application reads one buffered event. If none exists, it performs a guarded
capture and rereads. One event is applied with acknowledgement, applied
coordinate, cleanup, and `REPLAYING` settlement in one transaction. An empty
buffer moves `REPLAYING` to `STREAMING`; streaming remains streaming with its
configured database-clock polling delay.

## Error and lease behavior

- Schema/generation lock contention reschedules the same phase without
  consuming failure attempts.
- `dw_primary_key_conflict` enters `CONFLICT`; other non-retryable business
  errors enter `PAUSED`.
- Retryable business errors and transport failures use bounded durable retry;
  exhausted work enters `DEAD`.
- Unexpected errors persist only phase and exception type; row images and
  exception messages remain out of durable state and logs.
- Lease loss cancels the guarded operation, awaits its cleanup, and performs no
  later settlement.

## Compatibility

- Preserve every existing task phase, task identity, captured/snapshot/applied
  coordinate, cursor, retry/backoff, heartbeat, lock-order, DW DML, and
  readiness contract.
- Preserve the worker command and settings keys.
- Do not change HTTP/SSE contracts or frontend code.
- Do not add a database schema, bootstrap change, migration, history rewrite,
  or compatibility module for the retired service import path.

## Test strategy

### Application seam

Replace `tests/unit/data_sync/test_service.py` with tests that instantiate
in-memory port adapters and call only `dispatch_once()`. Cover:

- one-task claim budget;
- streaming backlog closes readiness by settling `REPLAYING`;
- capture persists events and coordinate through the durable port;
- saturated backfill drains bounded events and continues the existing cursor;
- backfill delay is durable and does not sleep in the dispatcher;
- lock contention reschedules without retry attempts;
- deterministic conflict/pause and transient/dead retry classification;
- generation/schema flow and lease cancellation through adapter contracts.

### External adapter contracts

Keep focused tests for MySQL materialization, source capture/decoding, schema
evolution, repository CAS, lock ordering, and the neutral Meta Projection input.
Those tests may inspect SQL/transaction behavior because the adapter contract is
their declared seam. Remove tests that call `_process`, `_capture`, `_retry`,
`_reschedule`, `_renew_lease`, `_with_lease_heartbeat`, or
`_synchronize_schema` on the application module.

### Integration

Keep the live Data Sync CDC and DW convergence scenarios. They remain the proof
that production adapters preserve source-to-DW behavior and coordinate
convergence.

## Rollback shape

The child is independently revertible to the reviewed Meta Projection commit.
If the ports cannot preserve an existing transaction or lock invariant, revert
the child rather than add parallel service paths, compatibility wrappers, or a
database migration.
