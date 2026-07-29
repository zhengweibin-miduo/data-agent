# JSON Null Preservation and Generation Serialization Design

## 1. Scope and Ownership

This task closes two active PR #66 findings without changing public APIs or the
durable synchronization schema.

- `data_sync.binlog` owns the compatibility boundary between
  `mysql-replication` ROW events and `SyncRowEvent`.
- `data_sync.locks` owns stable per-target generation lock identities.
- `infrastructure.mysql` owns connection-scoped MySQL advisory-lock lifecycle.
- `ddl_metadata.persistence.snapshots` is the generation publisher.
- `data_sync.service` is the DDL consumer and authority verifier.
- `data_sync.schema_sync` continues to own deterministic additive DDL and the
  existing per-target schema lock.

The two fixes remain one task because they close one PR review gate, share the
same backend quality and live-MySQL validation boundary, and must be pushed and
resolved together.

## 2. JSON SQL NULL Preservation

### 2.1 Confirmed dependency behavior

`mysql-replication==1.0.16` reads the ROW-event null bitmap inside
`RowsEvent._read_column_data()`. A present JSON column follows one of two paths:

1. null bitmap bit set -> the dependency returns Python `None`;
2. null bit clear -> `read_binary_json()` decodes JSON literal `null` and also
   returns Python `None`.

After that point, `none_sources` does not distinguish the two values. Therefore
`encode_row_value(value, json_value=True)` cannot repair the ambiguity.

### 2.2 Adapter boundary

Before first access to an actual third-party event's lazy `rows` property,
`decode_rows_event()` installs an instance-local compatibility adapter for the
dependency's name-mangled private
`RowsEvent._RowsEvent__read_values_name(column, null_bitmap,
null_bitmap_index, is_partial, cols_bitmap, unsigned, i)` method. The locked
dependency calls this method from `_read_column_data()` once per column at
`row_event.py:227-235`; the original implementation and JSON branch are at
`row_event.py:242-366`.

The adapter checks the information that is still available at that boundary:

- column is present in `cols_bitmap`;
- column type is `FIELD_TYPE.JSON`;
- the ROW-event null bitmap marks that column SQL-NULL.

Only that case returns a private `_SQL_NULL` sentinel. The adapter is attached
with `MethodType` under the exact mangled instance attribute, so the base
`_read_column_data()` lookup uses it without modifying the dependency class.
All other values delegate to the locked dependency's original decoder with the
complete original argument list:

- JSON binary literal `null` remains Python `None`;
- non-null JSON values remain unchanged;
- non-JSON SQL `NULL` retains the dependency's existing behavior;
- partial JSON update behavior remains delegated.

The adapter is attached only to the concrete event instance, avoiding a global
monkey patch across source clients. A guarded compatibility lookup and signature
regression fail explicitly if the locked dependency removes or changes the
required private decoder boundary.

The runtime already rejects `binlog_row_image != FULL` and captures only
`WriteRowsEvent`, `UpdateRowsEvent`, and `DeleteRowsEvent`. MINIMAL images and
`PartialUpdateRowsEvent` remain outside this task; the adapter still delegates
the existing `is_partial` argument unchanged.

### 2.3 Canonical encoding

`_encode_row()` maps:

- `_SQL_NULL` -> ordinary `encode_row_value(None, json_value=False)` -> durable
  SQL `NULL`;
- JSON decoder `None` -> `encode_row_value(None, json_value=True)` ->
  `{"$json":"null"}`.

No persisted event schema, Pydantic model, backfill codec, or DW writer changes.

### 2.4 Data flow

```text
ROW null bit set + JSON column
  -> private _SQL_NULL
  -> durable None
  -> DW SQL NULL

ROW null bit clear + binary JSON null
  -> Python None
  -> {"$json":"null"}
  -> DW JSON literal null
```

## 3. Shared Generation Serialization

### 3.1 Lock identity

`data_sync.locks.generation_lock_name(dw_database, target_table)` derives one
stable name from the binary target identity. It uses a SHA-256 digest with a
short readable prefix, rather than truncating raw identifiers, so distinct long
targets cannot collide and the UTF-8 name remains within MySQL's 64-byte limit.

All sources writing the same DW target use the same generation lock.

### 3.2 Advisory-lock lifecycle

`MySQLDatabase.advisory_locks(names, timeout_seconds)`:

1. validates, deduplicates, and byte-sorts names;
2. checks out one dedicated connection from the existing Meta MySQL engine;
3. acquires each `GET_LOCK` in deterministic order;
4. yields while retaining that physical connection;
5. releases acquired locks in reverse order on success, exception, or
   cancellation;
6. reports timeout through a typed infrastructure exception.

A dedicated owner connection is intentional. The protected business Session
may commit or MySQL DDL may auto-commit without releasing the advisory lock.
All participants still use the same MySQL server and lock namespace.

### 3.3 Publisher flow

`MetadataSnapshotService.persist()` computes and sorts the unique generation
locks for its desired targets before opening the managed business Session:

```text
acquire all target generation locks
  -> open managed Session
  -> expire memory
  -> synchronize Meta
  -> upsert desired generation
  -> upsert memory/outbox
  -> Session commit or rollback
  -> release generation locks
```

The outer lock scope is load-bearing: release before managed-Session exit would
reopen the check/commit race. Lock timeout is converted to a safe retryable
`DataAgentError` at `persist_snapshot`.

No source, DW, Binlog, or model call occurs in this scope.

### 3.4 Worker flow

The worker keeps source capability checks outside the generation lock. For
schema synchronization:

```text
acquire target generation lock
  -> open managed DDL Session
  -> create DataSyncRepository on the DDL Session
  -> re-check desired hash + lease authority on that Session
  -> acquire existing target schema lock
  -> inspect and plan
  -> pass a callback closed over that same repository/Session
  -> re-check authority in the same Session before each DDL
  -> execute auto-commit DDL
  -> re-inspect and settle phase
  -> Session commit or rollback
  -> release generation lock
```

The fixed lock order is:

```text
generation advisory lock -> schema advisory lock -> task-row operations
```

`DataSyncService._synchronize_schema()` replaces the current
`lambda: self._has_authority(task)` callback, which opens a second Session, with
a callback closed over `DataSyncRepository(ddl_session)`. The callback interface
can remain parameterless because it captures the correct Session explicitly.

The existing lease heartbeat may use separate short Sessions while a long DDL
runs. It does not publish a new generation and therefore does not acquire the
generation lock.

The existing schema lock remains connection-owned by the DDL Session and is
released in `DWSchemaSynchronizer.synchronize()` before that Session exits.
The outer generation lock remains held while schema-lock release, phase
settlement and DDL Session commit/rollback complete. A schema-lock release
failure rolls back/closes the DDL Session and then releases the generation lock;
it must not return a lock-owning connection to the pool.

Generation-lock contention is handled like schema-lock contention: release the
task lease and reschedule without consuming failure attempts.

### 3.5 Race outcomes

Old worker wins:

1. old worker acquires generation lock;
2. old authority remains stable while DDL auto-commits;
3. old worker settles and commits;
4. publisher acquires lock and commits the new desired generation.

Publisher wins:

1. publisher commits new desired generation under the lock;
2. old worker later acquires the lock;
3. authority re-check fails;
4. no old DDL executes and stale settlement is skipped.

There is no interval in which a new generation can commit between the old
authority check and old DDL execution.

## 4. Failure and Recovery

- Partial multi-lock acquisition releases already acquired locks.
- Business rollback completes before publisher lock release.
- DDL auto-commit remains irreversible, but its generation cannot change while
  the lock is held.
- Lock timeout is bounded; no caller waits indefinitely.
- A release failure invalidates or closes the owner connection so a pooled
  physical connection cannot retain a leaked lock.
- Existing DDL idempotency and post-DDL reinspection remain the retry mechanism.
- No rollback deletes Meta, `data_sync`, DW, or source data.

## 5. Compatibility

- Public HTTP, Redis, task-phase and answer-readiness contracts are unchanged.
- Durable `SyncRowEvent` JSON remains backward compatible.
- Existing JSON literal `null` events continue to decode identically.
- Existing schema lock remains distinct; Meta publication waits on the
  generation lock, not the DW schema lock.
- The implementation relies on the locked `mysql-replication==1.0.16` private
  decode signature and contains an executable compatibility regression.

## 6. Validation Strategy

Unit tests cover:

- exact mangled dependency-adapter signature, lazy first-row access and
  instance-local installation;
- SQL `NULL` sentinel versus JSON literal `null`;
- INSERT, UPDATE before/after, DELETE and non-JSON compatibility under the
  required FULL row image;
- deterministic lock naming;
- ordered acquisition, reverse release, partial failure, timeout and exception
  cleanup;
- worker authority re-check using the DDL Session.

Live MySQL tests cover:

- SQL `NULL` and JSON literal `null` source writes, capture and DW query
  semantics;
- publisher remains uncommitted while an old worker owns the generation lock,
  then either commits after release or returns the bounded retryable timeout;
- publisher-first ordering prevents stale DDL;
- exception paths release the lock;
- different targets remain independently executable.

Full repository quality gates remain required before push and thread
resolution.
