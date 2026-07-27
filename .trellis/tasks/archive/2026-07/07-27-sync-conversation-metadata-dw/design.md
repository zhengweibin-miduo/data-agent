# DW Schema and Binlog CDC Design

## 1. Scope and Boundaries

This task adds an asynchronous pipeline after an accepted DDL metadata snapshot:

1. materialize the accepted physical table and column shape in `dw`;
2. backfill existing source rows in bounded primary-key chunks;
3. replay buffered MySQL Binlog row events;
4. continue applying INSERT, UPDATE, and DELETE events.

The existing boundaries remain authoritative:

- `meta` owns table, column, relation, and metric definitions;
- `data_agent` owns conversation and long-term memory;
- `dw` owns business rows only;
- new schema `data_sync` owns synchronization control state, temporary Binlog events, offsets, retries, conflicts, and target-key ownership.

No public API is added. The existing DDL Job succeeds when the accepted Meta snapshot and durable synchronization request commit. DW work is asynchronous and cannot change a completed DDL Job.

## 2. Confirmed Invariants

- Only named MySQL sources are supported. `source` resolves server-side configuration and never carries credentials through HTTP, LLM prompts, Redis, or logs.
- Every synchronized source table declares a primary key in the submitted DDL. Missing keys reject the DDL Job before persistence.
- Physical names, types, and keys come from the SQLGlot AST. The LLM cannot invent or change them.
- One DW fact table may receive rows from multiple sources, but the target table name has no source prefix and rows have no source column.
- `data_sync` owns `(target_table, primary_key) -> source`. A second source colliding with an owned key enters conflict and cannot overwrite the row or advance its offset.
- Key ownership remains as a tombstone after a source DELETE so another source cannot silently reuse the key.
- Automatic DW evolution is additive: create table, add column, or perform a proven-safe widening. Drop, rename, narrowing, or ambiguous conversion pauses the table in a reviewable failure state.

## 3. Module Ownership

Add one root business feature, `src/data_agent/data_sync/`, rather than placing CDC behavior in Conversation or DDL workflow modules.

```text
src/data_agent/data_sync/
├── models.py          # typed desired schema, source event, offset, phase, conflict
├── tables.py          # schema-qualified data_sync control tables
├── repository.py      # short target-side transactions and claims
├── schema_sync.py     # deterministic DW create/add/widen planner and executor
├── backfill.py        # bounded primary-key keyset copy
├── binlog.py          # Binlog adapter and canonical row-event decoding
├── service.py         # phase/state orchestration
└── worker.py          # dedicated CDC process lifecycle
```

Shared configuration remains in `settings.py`; target MySQL lifecycle remains in `infrastructure/mysql.py`. Source connection and Binlog resources receive explicit lifecycle ownership and are not constructed per row or per event.

The CDC worker is a dedicated process. It does not run as a long arq job or cron callback, so Binlog polling cannot occupy DDL worker capacity.

## 4. Configuration

Add:

- `data_sync.database`, default `data_sync`;
- `data_sync.dw_database`, default `dw`;
- bounded claim lease, retry, batch size, batch interval, event-buffer limit, and dead-letter settings;
- a mapping of named MySQL sources containing a source URL and a unique replication `server_id`.

Validation requires:

- strict MySQL identifiers for `data_sync` and `dw`;
- both schemas differ from each other, `meta`, and `memory.database`;
- source URLs use MySQL;
- source names and `server_id` values are unique;
- positive bounded batch, lease, retry, and buffer values.

Deployment prerequisites for every source are `binlog_format=ROW`, `binlog_row_image=FULL`, sufficient Binlog retention, SELECT access to synchronized tables, and replication-client privileges. Startup fails safely when a configured source does not meet them.

## 5. Durable Handoff from Meta

Extend the accepted-snapshot transaction with a schema-qualified `data_sync` desired-state upsert. The row contains a typed, bounded desired-sync document derived from the accepted physical schema and semantic table roles:

- source;
- source schema/table;
- canonical DW table;
- physical columns and types;
- ordered primary-key columns;
- schema fingerprint;
- metric dependency column IDs.

This is a durable work projection, not a second metadata authority. Meta remains authoritative. A repeated accepted snapshot writes the same desired identity; a newer schema fingerprint supersedes pending work for the same `(source, source_table, target_table)`.

Writing the desired state in the same target MySQL transaction prevents a committed Meta snapshot from losing its asynchronous request. No DW DDL or source call occurs inside this transaction.

## 6. `data_sync` Persistence Model

Use the minimum durable tables needed for recovery:

### `data_sync_task`

One current desired state per `(source, source_schema, source_table, target_table)`.

Stores desired document/hash, phase, snapshot and applied Binlog coordinates, last completed backfill primary key, attempts, availability, lease token/deadline, safe last error type, and timestamps.

Phases:

`pending_schema -> buffering -> backfilling -> replaying -> streaming`

Operational terminal/holding states:

`paused`, `conflict`, `dead`.

### `data_sync_event`

Temporary row events retained while backfill or replay is incomplete. Event identity is source plus Binlog coordinate and row index. The unique identity makes capture replay idempotent.

Payloads use one typed codec for MySQL values, including decimal, temporal, binary, and null values. Consumers do not cast event JSON independently.

### `data_sync_key_owner`

Unique target identity keyed by a collision-resistant hash plus the canonical primary-key document. It stores target table and owning source. Hash matches must re-check the full primary-key document before use.

All three tables are schema-qualified to `data_sync` and use the existing target MySQL engine. Business-row DML, key ownership, event acknowledgement, and offset advancement can therefore commit atomically across `dw` and `data_sync` when they contain only InnoDB DML.

## 7. DW Schema Materialization

Use accepted physical schema, never model prose, to build the target shape.

The planner introspects the current DW table and emits only:

- `CREATE TABLE` when absent;
- `ADD COLUMN` for a missing accepted column;
- `MODIFY COLUMN` for a proven-safe widening.

The initial conservative widening matrix supports:

- integer-family widening toward `BIGINT`;
- `VARCHAR`/`VARBINARY` length increase;
- `DECIMAL(p,s)` only when both integer capacity and fractional scale do not decrease.

Exact matches are no-ops. Every other difference is a non-retryable schema conflict. Identifiers are validated and quoted by SQLAlchemy/MySQL dialect machinery; no LLM or request string is interpolated into SQL.

MySQL DDL auto-commits, so each statement is individually idempotent and the task re-introspects before every retry. Data phases do not start until the full desired shape is present.

## 8. Low-Impact Baseline and CDC

### 8.1 Capture

Before backfill, record the source Binlog coordinate and start capturing ROW events for the selected tables into `data_sync_event`. Capture continues while history is copied.

### 8.2 Backfill

Read source rows with primary-key keyset pagination:

- ordered primary-key tuple;
- configured maximum rows per batch;
- configured pause between batches;
- one committed target batch at a time;
- durable last-completed key after the DW batch commits.

Composite primary keys use lexicographic tuple pagination. No whole-table transaction, offset pagination, or unbounded result is allowed. Restart resumes after the last completed key.

Each row first claims target-key ownership. A different owner produces `conflict`, leaves DW unchanged, and does not advance progress beyond the conflicting row.

### 8.3 Replay and Streaming

After the last backfill chunk:

1. replay buffered events in Binlog order;
2. apply INSERT/UPDATE with MySQL upsert by primary key;
3. apply DELETE by primary key;
4. atomically settle key ownership, event acknowledgement, and applied coordinate with the DW row mutation;
5. switch to streaming when the buffer reaches the live capture tail;
6. remove acknowledged temporary events in bounded batches.

At-least-once delivery is expected. Stable event identity, primary-key upsert/delete, ownership checks, and post-write coordinate advancement make retries converge.

## 9. Claim, Retry, and Failure Semantics

Reuse the repository's established three-phase worker pattern:

1. short `FOR UPDATE SKIP LOCKED` claim and lease commit;
2. source/Binlog work outside row-lock transactions;
3. short per-task settlement transaction that revalidates desired identity and lease.

Retryable transport failures use bounded exponential backoff. Lease expiry does not consume retry budget. Deterministic schema or ownership conflicts do not retry forever; they retain the row in `conflict`. Exhausted transient failures retain the row in `dead`.

Offsets advance only after the matching DW write commits. A stale worker cannot settle a superseded schema hash or lost lease.

## 10. Bootstrap and Deployment

Add `docs/docker/mysql/data_sync.sql` for fresh local bootstrap and keep SQLAlchemy Core definitions aligned. Existing initialized volumes are not upgraded automatically.

Local MySQL Compose must enable ROW Binlog, FULL row images, a unique server ID, and suitable retention. Add a dedicated local replication user with only required replication and SELECT privileges for the local source fixture.

Add one direct Python Binlog client dependency. Kafka and Debezium are deliberately excluded because the repository has neither infrastructure and the direct daemon satisfies the current source count and durability contract.

## 11. Compatibility, Rollback, and Security

- Existing HTTP request/response models and DDL Job statuses remain compatible.
- Existing Meta and memory persistence remains atomic; only the durable desired-sync write joins that transaction.
- Disabling the CDC process stops DW convergence but does not affect DDL Job execution.
- Rollback is stopping the CDC worker and disabling task creation. Existing DW rows and `data_sync` state are retained for inspection; no automatic destructive rollback runs.
- Logs contain source name, table, phase, bounded counts, attempt, and safe error type. They never contain source URLs, passwords, raw DDL, or row payloads.

## 12. Validation Strategy

Unit checks cover:

- required primary-key validation;
- safe widening matrix and destructive-diff rejection;
- typed Binlog event codec;
- phase transitions, lease ownership, backoff, and stale settlement;
- multi-source target-key ownership conflicts;
- keyset pagination for simple and composite primary keys.

Live MySQL integration checks cover:

- fresh bootstrap creates `data_sync`;
- Meta snapshot atomically enqueues desired sync work;
- schema create/add/widen idempotency;
- bounded backfill resume;
- INSERT/UPDATE/DELETE Binlog convergence;
- crash after DW write but before settlement;
- offset never advances on failed write;
- two sources writing one fact table without collision;
- cross-source primary-key collision preservation;
- replay-to-stream transition and temporary event cleanup.
