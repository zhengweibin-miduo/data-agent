# Database Guidelines

## Current Database Scope

MySQL is the only relational database currently wired into the application.
The project uses SQLAlchemy 2's async engine and `AsyncSession` with the
`asyncmy` driver. The DSN is validated by `MySQLSettings` in
`src/data_agent/settings.py`; engine and Session-factory lifecycle is owned by
`src/data_agent/infrastructure/mysql.py`.

The DDL metadata feature uses SQLAlchemy Core table definitions in
`ddl_metadata/persistence/tables.py` for Meta snapshots and
`memory/mysql/tables.py` for long-term memory. Conversation has its own table
definitions under `conversation/mysql_tables.py`. All three import the single
`MetaData` owner from `data_agent/persistence/schema.py`. The project
deliberately does not add ORM entities or a migration framework.
`MetadataRepository` owns the four Meta snapshot tables; `DataSyncRepository`
owns schema-qualified tasks, Binlog event buffers, offsets, leases, retries,
and DW key ownership in `data_sync`; `MemoryRepository`
owns authoritative records, append-only events, typed links, and browser
mutations; `MemoryIndexOutboxRepository` owns derived-index desired state,
claiming, retry, acknowledgement, projections, and rebuild scans. The Meta
tables use the default database in `mysql.url`; data-sync control tables use
`data_sync.database`, DW business rows use `data_sync.dw_database`, and all
four memory tables are
schema-qualified to `memory.database` (`data_agent` by default). They still
share one engine and caller-owned Session so MySQL commits both InnoDB
databases and record-plus-outbox changes atomically.

## Engine Lifecycle

Follow the existing database lifecycle instead of constructing engines at each
call site:

```python
class MySQLDatabase:
    _client: ClassVar[AsyncEngine | None] = None
    _session_factory: ClassVar[async_sessionmaker[AsyncSession] | None] = None

    @classmethod
    def initialize(cls) -> AsyncEngine:
        if cls._client is None:
            cls._client = create_async_engine(
                app_config.mysql.url,
                pool_pre_ping=True,
                pool_recycle=3600,
            )
            cls._session_factory = async_sessionmaker(
                bind=cls._client,
                expire_on_commit=False,
            )
        return cls._client
```

`initialize()` is idempotent, `get_client()` requires prior initialization,
and `close()` detaches the current engine and Session factory before awaiting
`AsyncEngine.dispose()`. This preserves a replacement created concurrently
while the old engine is disposing.

## Session and Transaction Lifecycle

Business code uses the manager-owned async context instead of sharing an
`AsyncSession` or repeating commit/rollback boilerplate:

```python
async with MySQLDatabase.session() as session:
    await session.execute(statement)
```

Each context receives a fresh Session bound to the shared engine. Normal exit
commits; exceptional exit rolls back and re-raises the original exception;
the Session closes on both paths. The factory uses `expire_on_commit=False`.

### Row locks must never span an external call

A transaction that holds row locks while calling Elasticsearch, Qdrant, TEI, or
a model endpoint blocks every writer of those rows for the duration of the
remote call. Because all authoritative writers touch `memory_index_outbox` and
`conversation_memory_outbox` inside their own transactions, one slow remote call
turns into user-visible write latency or `lock_wait_timeout` failures.

Background workers that combine database claims with remote calls use three
phases instead of one transaction:

1. **Claim (short transaction).** Select claimable rows `FOR UPDATE SKIP LOCKED`
   and immediately write a lease, then commit to release the locks. The lease is
   expressed with existing columns — push `available_at` forward for
   `memory_index_outbox`, write `lease_token` plus `lease_expires_at` for
   `conversation_memory_outbox` — so a crashed worker's rows become claimable
   again when the lease expires, with no separate recovery channel.
2. **Remote work (no transaction).** Perform the external calls.
3. **Settle (one short transaction per item).** Acknowledge or back off.

Because the lock is gone during phase 2, the claimed state may be stale by the
time phase 3 runs. Settlement must therefore re-verify authority rather than
assume the claim still holds:

- Acknowledgement matches the full desired-state identity
  (`memory_uid`, `target`, `operation`, `projection_version`) **and** asserts the
  authoritative row still agrees with what was actually written — the same
  `content_hash` and `status = ACTIVE` for a write, or no ACTIVE row at all for a
  delete. A concurrent content change writes a fresh desired state with the same
  identity; acknowledging on identity alone would retire that new request and
  leave the stale payload in the derived index permanently.
- Back-off matches the full desired-state identity too, so a late worker cannot
  postpone a desired state that has already been overwritten.
- A failed consistency check is not a failure: leave the row untouched and let
  the next cycle re-process it. Do not consume the retry budget for it.

### Timers belong to the database

Claim conditions compare against `func.now()`, so any deadline written by the
application must come from the same clock. Compute lease and back-off deadlines
with `func.timestampadd(text("SECOND"), seconds, func.now())`; never write
`datetime.now(UTC).replace(tzinfo=None) + timedelta(...)`. A database session in
a non-UTC time zone silently shifts naive Python deadlines, which either makes
exponential back-off fire immediately or defers work by hours.

### Every outbox needs a dead-letter bound

Deterministic failures (a vector dimension mismatch, a payload the index
rejects) never succeed on retry. Claim queries therefore exclude rows whose
`attempts` reached the configured maximum. Such rows stay in the table so they
keep shadowing stale search hits and keep blocking physical purge, and the
dispatcher logs the backlog size — silence must not be mistaken for success.
Only remote-call failures increment `attempts`; lease expiry and superseded
settlements must not.

### Re-verify a lockless scan under lock before acting on it

A cursor scan that runs without locks (`scan_active`) may return rows that a
concurrent transaction deletes before the scan's result is used. Any write
derived from such a scan re-selects the rows `FOR UPDATE` inside the writing
transaction and acts only on those still matching the required status. Rebuild
does this so a scanned-then-deleted UID cannot have its committed DELETE desired
state overwritten back into an UPSERT.

## Repository and Query Pattern

Health checks use SQLAlchemy `text()` for a small connection probe:

```python
async with client.connect() as connection:
    assert await connection.scalar(text("SELECT 1")) == 1
```

Production persistence uses static SQLAlchemy Core `Table` objects and bound
statements. A repository receives the caller's `AsyncSession` and never commits
or closes it:

```python
async with MySQLDatabase.session() as session:
    await MetadataRepository(session).synchronize(schema, metadata, metrics)
    await MemoryRepository(session).upsert_candidates(memories)
```

Contracts:

- Table and column identifiers come from
  `data_agent.ddl_metadata.persistence.tables`, never interpolated request or
  model output.
- Idempotent snapshot and outbox desired-state writes use
  `sqlalchemy.dialects.mysql.insert()` with explicit update columns. Immutable
  authority versions use plain inserts after lifecycle comparison; duplicates
  become audited `NOOP` decisions instead of in-place content updates.
- Repository methods accept and return typed application contracts or bounded
  row projections; JSON is decoded through the central memory parser.
- Service code owns the transaction boundary. Repositories can share one
  Session when Meta rows and trusted memories must commit atomically.
- Do not create a second engine or Session for the application memory
  database. Static schema-qualified memory tables keep the cross-database
  transaction on one MySQL connection.
- Read-only service calls still use the managed Session context; its normal
  exit commits an otherwise empty transaction.

## Scenario: Atomic Meta Snapshot and Agent Memory

### 1. Scope / Trigger

Use this contract when changing the four Meta tables, authoritative memory,
history, links, index outbox, user updates, deletion, or accepted-snapshot persistence.

### 2. Signatures

```python
await MetadataSnapshotService.persist(snapshot, memory_candidates) -> SnapshotResult
await MemoryService.update(memory_uid, content) -> MemoryUpdateResponse
await MemoryIndexRebuilder.enqueue_batch(after_id=0) -> MemoryRebuildResult
```

### 3. Contracts

`MetadataSnapshotService.persist()` is the only accepted-snapshot commit
boundary. In
one managed MySQL transaction spanning the default Meta database and the
configured application memory database it:

1. upserts submitted table, column, metric, and column/metric rows;
2. removes stale links and columns only for submitted table IDs;
3. removes metrics that became orphaned through that scoped cleanup;
4. applies explicit `ADD/UPDATE/MERGE/DELETE/NOOP` decisions, version history,
   typed links, and the database-unique active slot; and
5. writes Elasticsearch and Qdrant desired operations to `memory_index_outbox`.

Stable SHA-256 content IDs and the unique memory-link triple make re-execution
safe after a worker crash. When content whose base UID is `SUPERSEDED` or
`EXPIRED` becomes current again, the repository derives a new UID from that base
UID plus the next `record_version`, remaps same-batch `DERIVED_FROM` and
`RELATED` references, supersedes the current active row, and emits normal
DELETE/UPSERT projection work. Replaying an accepted snapshot must not
reactivate a UID that a user already soft-deleted; its DELETE projection remains
the desired outbox state. Tables absent from the submitted DDL are outside
cleanup scope.
No repository write is permitted while a graph waits for user input or before
deterministic validation succeeds.

DDL-scoped memory updates acquire the logical-source mutation lease, then use a
separate managed transaction to validate category, stable memory key, expected
record version, and current Meta references. They immediately create a new
user-confirmed ACTIVE memory version, mark the old row SUPERSEDED, and return
`requires_reprocess=true`; they do not patch Meta directly, so the next complete
DDL workflow must consume that authority before Meta changes. User-scoped
conversation-memory updates lock only the user-owned authority row, create the
new ACTIVE version immediately, return `requires_reprocess=false`, and do not
acquire the DDL source lease. DELETE is an audited soft delete plus two DELETE
outbox rows; only DDL-scoped deletion acquires the source lease. Exact duplicate
content records NOOP history but creates no version or projection work.

`agent_memory.memory_key` is bounded to 256 characters. Its exact-lookup
composite index also contains source, category, fingerprint, and status; a 512
character key exceeds InnoDB's 3072-byte index limit under `utf8mb4`.

Derived-index rebuilding reads at most `memory.rebuild_batch_size` ACTIVE rows
after an explicit numeric cursor and writes two UPSERT outbox rows per UID.
Rebuild explicitly recreates only the configured project ES index and Qdrant
collection; MySQL content is never reconstructed from an index payload.

Exact metric-memory reuse batch-loads outgoing `DERIVED_FROM` semantic targets
from active metric definitions. Raw metric questions and answers remain typed
evidence inside the final `ddl.metric` content; they are not separate durable
memory rows and cannot independently become current graph context.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Graph is waiting or validation is incomplete | Write no Meta or trusted memory rows |
| Snapshot statement fails | Roll back all Meta, memory, event, link, and outbox changes |
| Schema-qualified memory statement fails after Meta writes | Roll back the earlier Meta writes in the same transaction |
| Same accepted snapshot is replayed | Stable IDs and unique relations produce the same state |
| `SUPERSEDED` or `EXPIRED` content becomes current again | Create a new version UID, keep one ACTIVE row, and advance `record_version` |
| A soft-deleted UID appears in a replayed snapshot | Keep it DELETED and retain DELETE outbox desired states |
| Submitted table drops a column | Remove stale rows only inside submitted table IDs |
| Update repeats identical user-confirmed content | Return `unchanged_update`; append no event |
| Derived index is missing, stale, or corrupt | Rebuild from MySQL; never infer trust from index payload |

### 5. Good / Base / Bad Cases

- Good: accepted Meta, authoritative memories, events, links, and outbox
  commit together after deterministic validation.
- Base: browser update appends a trusted event and requires a later DDL run
  before Meta changes.
- Bad: repository methods commit independently, or update edits current
  Meta rows without full DDL validation.

### 6. Tests Required

```powershell
uv run pytest tests/integration/persistence
uv run pytest tests/integration/test_memory_services.py
uv run pytest tests/integration/test_ddl_metadata_flow.py
```

Tests must assert full rollback, unrelated-table preservation, repeat
idempotency, update no-op behavior, outbox replay, link-aware reuse, soft
deletion, post-commit replay safety, and A→B→A reactivation with the old A and B
both `SUPERSEDED`, the new A uniquely `ACTIVE`, and matching DELETE/UPSERT
outbox operations.

### 7. Wrong vs Correct

```python
# Wrong: separate transactions can leave Meta and memory inconsistent.
await meta_repository.persist(snapshot)
await memory_repository.persist(candidates)

# Correct: both repositories receive the same managed Session.
async with MySQLDatabase.session() as session:
    await meta_repository.persist(session, snapshot)
    await memory_repository.persist(session, candidates)
```

## Scenario: Asynchronous DW Materialization and CDC State

### 1. Scope / Trigger

Use this contract when changing accepted-snapshot handoff, DW DDL, historical
backfill, Binlog replay, source collision handling, or `data_sync` persistence.

### 2. Signatures

```python
await DataSyncRepository(session).upsert_desired(desired_tables) -> None
MySQLDatabase.advisory_locks(names, timeout_seconds=10) -> AsyncIterator[None]
generation_lock_name(dw_database, target_table) -> str
await DWSchemaSynchronizer(session, database="dw").synchronize(
    desired,
    check_authority=authority_callback,
) -> None
await apply_backfill_batch(session, task, rows, dw_database="dw")
await apply_buffered_event(session, task, event, dw_database="dw") -> None
```

### 3. Contracts

- `meta` stores definitions, `dw` stores business rows, and `data_sync` stores
  only tasks, leases, retries, Binlog coordinates/events, backfill cursors, and
  `(target_table, primary_key) -> source` ownership.
- Accepted Meta rows and `data_sync` desired state commit in the same managed
  MySQL Session. DDL Job success does not wait for DW work.
- A task identity is unique by source, source schema/table, and target table;
  `(source, target_table)` is also unique so concurrent snapshots cannot map two
  physical tables from one named source onto the same DW table. Claims use
  database-clock leases and compare desired hash plus lease token when settling.
- DW evolution permits create table, add column, and safe type widening only.
  Destructive or ambiguous differences pause work without altering Meta.
- Initial load records a Binlog coordinate, persists later ROW events, reads
  source rows by bounded simple/composite-PK keyset, replays the buffer, then
  enters streaming. If the durable event buffer fills before the historical
  scan completes, the worker applies a bounded persisted event before reading
  the next current source batch and retains the completed primary-key cursor;
  it must neither leave a full buffer parked in `backfilling` nor repeatedly
  discard the baseline under sustained writes.
- Per-target DW schema locks serialize inspection and additive DDL. Lock
  contention is normal scheduling pressure: release the task lease and delay
  the same phase without incrementing its failure attempts or moving it toward
  `dead`.
- Accepted-snapshot publishers and DDL workers share one generation advisory
  lock derived from the binary `(dw_database, target_table)` identity. A
  publisher acquires every target lock in deterministic UTF-8 byte order before
  opening its managed transaction and releases them only after commit or
  rollback. A worker holds the same lock across its DDL Session, schema-lock
  acquisition, same-Session authority checks, auto-commit DDL, phase settlement,
  and managed Session commit. The global order is `generation -> schema ->
  task`; no path may acquire those resources in reverse.
- `MySQLDatabase.advisory_locks()` owns locks on one dedicated engine
  connection, independent of the protected business Session. It releases
  acquired locks in reverse order. A partial acquisition releases the earlier
  locks; a release failure invalidates the owner connection so a connection
  carrying an advisory lock cannot return to the pool. The YAML key
  `data_sync.generation_lock_timeout_seconds` is an integer in `1..300`.
- The DDL Session uses `READ COMMITTED` and one `DataSyncRepository` for every
  authority check and final phase settlement. The schema synchronizer checks
  authority once after acquiring the schema lock and again immediately before
  every DDL statement. A zero-row settlement is lease loss, not success.
- `mysql-replication==1.0.16` lazily decodes ROW values through the private
  `_RowsEvent__read_values_name(self, column, null_bitmap,
  null_bitmap_index, is_partial, cols_bitmap, unsigned, i)` boundary. Before
  first access to `event.rows`, the capture adapter must preserve JSON-column
  SQL `NULL` with a private in-process sentinel while delegating all other
  values to the locked dependency. Durable encoding maps that sentinel to
  ordinary `None` and maps binary JSON literal `null` to
  `{"$json": "null"}`. Never infer the distinction from the decoded Python
  value alone because both sources otherwise decode to `None`.
- Target DML, key ownership, event acknowledgement, and applied-coordinate
  advancement share one MySQL transaction. Captured and applied coordinates are
  separate. When a streaming capture persists new events, the same transaction
  advances the captured coordinate and changes the task to `replaying`, so
  readiness can never observe a committed backlog with a streaming phase.
- The first source to write a target primary key owns it permanently, including
  after DELETE. Another source conflicts before DW DML and cannot advance the
  event. Historical backfill claims and verifies a whole bounded batch of key
  owners with a constant number of database statements before its bulk upsert;
  it must not perform ownership I/O once per row. Type-aware primary-key
  identity normalization is shared by source backfill values, CDC values, and
  DW provenance scans; container-backed MySQL types such as `BIT` and `SET`
  must encode identically in every path. Accepted tables reject `ENUM`, `SET`,
  `FLOAT`, and `DOUBLE` primary keys because keyset ordering or MySQL numeric
  equivalence cannot be represented reliably by the durable cursor and
  ownership document. Non-key `ENUM` and `SET` declarations retain their full
  validated type text rather than an arbitrary per-column 255-character cap.
- Answer readiness uses `DataSyncRepository.read_readiness_phases()` as a
  separate read-only boundary. It selects only `phase` and the dedicated
  `worker_heartbeat_at`, takes no lock, and does not claim, renew, settle,
  retry, or update a task. Only worker settlement and lease renewal refresh
  that heartbeat; snapshot handoff may update `updated_at` but must not refresh
  liveness. A source-scoped dependency must match exactly one task; an unscoped
  dependency requires every matching task to be `streaming`. Missing or
  ambiguous matches are not ready.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Accepted table has no primary key | Reject before Meta or desired-state writes |
| Primary key uses `ENUM`, `SET`, `FLOAT`, or `DOUBLE` | Reject before Meta or desired-state writes |
| Missing target table/column or safe widening | Apply idempotently |
| Drop, rename, narrowing, PK drift, or incompatible type | Pause with a safe deterministic error |
| Backfill batch fails | Roll back DW rows, ownership, and cursor |
| Binlog event fails | Roll back DW DML, acknowledgement, and applied coordinate |
| Cross-source primary-key collision | Preserve existing DW row and enter `conflict` |
| Lease expires or desired hash changes | Stale settlement updates zero rows |
| Publisher or worker cannot acquire a generation lock in time | Retry/reschedule without publishing partial state or consuming the worker failure budget |
| Generation-lock acquisition stops after some targets | Release every previously acquired lock in reverse order |
| Advisory- or schema-lock release fails | Invalidate the owner connection before it can return to the pool |
| DDL worker loses authority after acquiring the schema lock | Release locks and execute no later DDL or phase settlement |
| Locked `mysql-replication` private decoder signature changes | Fail fast at import/startup; do not silently collapse JSON SQL `NULL` |

### 5. Good / Base / Bad Cases

- Good: Meta plus desired state commits, the worker creates DW structure,
  keyset-backfills, preserves JSON null provenance, replays buffered ROW events,
  and reaches streaming while publisher/worker generation changes serialize.
- Base: replaying identical desired state or a duplicate event is idempotent;
  generation-lock contention only delays the same phase.
- Bad: put cursors in `dw`, business rows in `data_sync`, hold a transaction
  while reading the source, check authority in a second Session before
  auto-commit DDL, or advance the applied coordinate before DW commit.

### 6. Tests Required

```powershell
uv run pytest tests/unit/data_sync
uv run pytest tests/integration/data_sync
uv run pytest tests/integration/answer_readiness
```

Tests assert DDL idempotency and widening rules, composite-PK continuation,
INSERT/UPDATE/DELETE convergence, captured/applied offset separation,
cross-source collision without overwrite, retry/dead-letter state, and scoped
cleanup. Advisory-lock tests assert deterministic acquisition, reverse release,
partial-acquisition cleanup, active-exception preservation, and owner-connection
invalidation. ROW-decoder tests pin the private dependency signature and cover
SQL `NULL` versus JSON literal `null` for INSERT, UPDATE before/after images, and
DELETE. A live MySQL ROW/FULL integration test must carry both null forms through
capture, durable event storage, replay, and DW assertions using `IS NULL` and
`JSON_TYPE`.

### 7. Wrong vs Correct

```python
# Wrong: a separate commit can publish Meta without recoverable desired state.
await metadata_repository.synchronize(snapshot)
await data_sync_repository.upsert_desired(desired)

# Correct: one accepted-snapshot transaction owns both writes.
async with MySQLDatabase.session() as session:
    await MetadataRepository(session).synchronize(...)
    await DataSyncRepository(session).upsert_desired(desired)
```

```python
# Wrong: the publisher can commit a newer generation after this check and
# before the worker's auto-commit DDL.
if await authority_repository.has_authority(task):
    await ddl_session.execute(ddl)

# Correct: publisher and worker share the same outer generation lock; the
# worker rechecks authority inside the schema lock with its DDL Session.
async with MySQLDatabase.advisory_locks(
    [generation_lock_name(dw_database, target_table)],
    timeout_seconds=settings.generation_lock_timeout_seconds,
):
    async with MySQLDatabase.session() as ddl_session:
        repository = DataSyncRepository(ddl_session)
        await DWSchemaSynchronizer(ddl_session, database=dw_database).synchronize(
            desired,
            check_authority=lambda: repository.has_authority(task),
        )
        await repository.settle_phase(task, SyncPhase.BUFFERING)
```

## Schema and Migrations

No migration tool or migration directory exists. Local MySQL bootstrap
initializes `data_agent`, `data_sync`, `dw`, `meta`, and the local
`source_demo` database from
`docs/docker/mysql/`. `meta.sql` contains only `table_info`, `column_info`,
`metric_info`, and `column_metric`; `data_agent.sql` defines the fresh
conversation schema plus `agent_memory`, `agent_memory_event`,
`agent_memory_link`, and `memory_index_outbox`. `data_sync.sql` owns the three
CDC control tables; `source_demo.sql` owns only the local source database and
replication-user grants. SQLAlchemy Core definitions must
remain compatible with those bootstrap schemas. Integration fixtures may call
`metadata.create_all()` using the default `meta` connection because the memory
tables are schema-qualified, but they are not a production migration
mechanism.

## Scenario: Local MySQL Bootstrap Scripts

### 1. Scope / Trigger

Use this contract when adding or changing local MySQL bootstrap SQL under
`docs/docker/mysql/`. These scripts provide disposable local sample data; they
are not production migrations.

### 2. Signatures

The Compose service exposes the bootstrap directory through this read-only
mount while retaining the persistent data volume:

```yaml
volumes:
  - mysql_data:/var/lib/mysql
  - ./mysql:/docker-entrypoint-initdb.d:ro
```

The current script order is lexical: `data_agent.sql`, `data_sync.sql`,
`dw.sql`, `meta.sql`, then `source_demo.sql`.

### 3. Contracts

- Host source: `docs/docker/mysql/` relative to the Compose file.
- Container target: `/docker-entrypoint-initdb.d`, mounted read-only.
- Execution boundary: the official `mysql:8.4` entrypoint processes the scripts
  only when `/var/lib/mysql` is uninitialized.
- Persistence: normal restarts reuse `mysql_data` and do not rerun the scripts.
- Identity: `MYSQL_USER=data_agent`; application grants target
  `'data_agent'@'%'`. The local source additionally creates
  `'data_agent_replica'@'%'` with `SELECT`, `REPLICATION SLAVE`, and
  `REPLICATION CLIENT` only.
- Databases: the bootstrap scripts create `data_agent`, `data_sync`, `dw`,
  `meta`, and `source_demo`;
  Compose does not set `MYSQL_DATABASE`.
- Documentation: every bootstrap `CREATE TABLE` has a Chinese table-level
  `COMMENT`, and every business column has a Chinese column-level `COMMENT`.
  Comments explain business meaning without changing types, constraints,
  indexes, foreign keys, seed data, or statement order.
- Ownership: `meta.sql` owns exactly the four Meta tables. `data_agent.sql` uses
  InnoDB and owns the application conversation tables plus exactly four
  long-term-memory lifecycle tables; it does not manage retired memory
  contracts. `data_sync.sql` owns only `data_sync_task`, `data_sync_event`, and
  `data_sync_key_owner`; `source_demo.sql` owns no application control tables.
- Binlog: local Compose enables a nonzero server ID, ROW format, FULL row image,
  a named binary log, and bounded retention.
- Existing volume: entrypoint scripts do not rerun. Applying `data_agent.sql`
  explicitly can create missing objects but cannot upgrade an incompatible old
  memory schema. Reprovision only the exact confirmed application-memory
  targets in an approved environment; never touch Meta tables.

### 4. Validation & Error Matrix

| Condition | Expected result |
|-----------|-----------------|
| Empty `mysql_data` | Execute all five scripts in lexical order |
| Initialized `mysql_data` | Skip bootstrap scripts and preserve data |
| Missing init-directory mount | Start MySQL without creating the five local databases |
| `GRANT` user differs from `MYSQL_USER` and does not exist | Initialization fails at `GRANT` |
| A table or business column lacks a Chinese `COMMENT` | Static bootstrap review fails before merge |
| Compose cannot resolve `./mysql` | `docker compose config` or startup reports the invalid mount |
| Memory database equals the `mysql.url` default | Configuration validation fails before startup |

### 5. Good / Base / Bad Cases

- Good: an empty disposable volume creates `data_agent`, `data_sync`, `dw`,
  `meta`, and `source_demo`; the
  application user can access each owned schema, Meta contains only its four
  business tables, and database inspection exposes Chinese table and column
  meanings.
- Base: restarting an initialized local container leaves all database contents
  unchanged.
- Bad: forcing the scripts to run on every startup can execute their
  `DROP TABLE` statements and destroy local data; adding a new uncommented
  table also leaves the disposable schema undocumented.

### 6. Tests Required

- Run `docker compose -f docs/docker/docker-compose.yml config` and assert that
  both `/var/lib/mysql` and the read-only `/docker-entrypoint-initdb.d` mount
  are present.
- Assert application grants target `'data_agent'@'%'`; the replica account has
  source `SELECT` plus replication privileges and no source DDL/DML grants.
- Statically assert that every bootstrap table and business column carries a
  non-empty Chinese `COMMENT`; compare SQL tokens with comments removed when a
  comment-only task must prove that no schema or seed-data behavior changed.
- Run the repository checks against the default Meta URL after applying
  `data_agent.sql`; assert the SQLAlchemy memory objects use
  `schema=memory.database` and a forced memory-side constraint failure rolls
  back preceding Meta writes.
- When Docker is available and initialization behavior changes, use a
  disposable project/volume to assert that all five local databases
  exist after the first healthy startup. Never delete the developer's shared
  `mysql_data` volume for this check.

### 7. Wrong vs Correct

Wrong: the SQL grants access to a user that Compose does not create.

```sql
GRANT ALL PRIVILEGES ON dw.* TO 'atguigu'@'%';
```

Correct: the SQL reuses the configured local application user.

```sql
GRANT ALL PRIVILEGES ON dw.* TO 'data_agent'@'%';
```

Wrong: a bootstrap table leaves its purpose and fields undocumented.

```sql
CREATE TABLE example_table (id BIGINT PRIMARY KEY);
```

Correct: MySQL persists both table-level and column-level Chinese comments.

```sql
CREATE TABLE example_table
(
    id BIGINT PRIMARY KEY COMMENT '示例编号'
) COMMENT = '示例表';
```

## Scenario: Permanent Conversations and User Memory

Conversation history is authoritative in schema-qualified MySQL tables
`agent_conversation`, `agent_message`, and `conversation_memory_outbox`.
Messages are text-only and keyset-paged by their auto-increment `id`; Redis
checkpoints never store conversation history. Starting a turn persists the
user message and acquires the conversation's single `active_turn_uid`.
Completing it commits the assistant message, extraction outbox row, and gate
release in one transaction.

The `active_turn_uid` gate is leased, not permanent. `complete_turn` is the only
release path, so a caller that dies between start and completion — or loses its
`turn_uid` — would otherwise lock the conversation out of every future turn with
`conversation_busy` until someone edits the database. Because `start_turn` writes
`active_turn_uid` and `updated_at` in the same statement, `updated_at` is the
occupancy start and no extra column is needed. `start_turn` therefore treats the
gate as claimable when it is unset, already held by the same `turn_uid`, or older
than `conversation.turn_lease_seconds`. The staleness comparison is pushed into
SQL with `func.timestampadd(text("SECOND"), -lease, func.now())` for the same
reason back-off deadlines are: a naive Python timestamp compared against a
database clock in another time zone either expires the lease immediately or never.
A preempted turn's later `complete_turn` still fails its `active_turn_uid` check,
so it cannot overwrite the turn that replaced it.

Conversation-derived `agent_memory` rows use
`source=data_agent_conversation`, a non-null `user_id`, nullable
`created_job_id`, and explicit conversation/message provenance. DDL memory
rows retain `user_id IS NULL`. Every user-memory read, mutation, exact lookup,
hybrid-search authority check, and supersede lookup includes the same
`user_id` predicate. User deletion tombstones memory and leaves DELETE desired
states until both projection targets acknowledge them; only then may the
worker physically remove memory, links, and events. Previously soft-deleted
user memories also enter purge, and user mutations lock the authority row so
they cannot resurrect data after user deletion.

`docs/docker/mysql/` owns fresh bootstrap only. Its SQL files contain database
and table creation definitions, not `ALTER TABLE`, data updates, or upgrade
scripts for initialized environments. This repository does not provide a
database migration framework; an existing incompatible local environment must
be reprovisioned from the current bootstrap before the project-owned ES index
and Qdrant collection are recreated. Rebuild advances each scanned ACTIVE
authority row to the configured projection version while enqueueing both
targets.

## Configuration and Naming

- The YAML key is `mysql.url` in `conf/app_config.yaml`.
- The YAML key `memory.database` selects the application-owned memory database,
  defaults to `data_agent`, accepts only strict ASCII MySQL identifiers, and
  must differ case-insensitively from the default database in `mysql.url`.
- The URL uses `mysql+asyncmy://` and is typed as `str` by `MySQLSettings`.
- Local Compose and CI use `data_agent` as the application user and `meta` as
  the application's default database.
- Do not duplicate the DSN in client modules; read it from the shared
  `app_config` instance.

## Common Mistakes

- Do not use SQLAlchemy's synchronous engine in async application code.
- Do not create an engine per query; reuse `MySQLDatabase`.
- Do not share one `AsyncSession` across concurrent tasks; enter a new
  `MySQLDatabase.session()` context per transaction.
- Do not clear shared manager state after awaiting disposal; detach the old
  state first so concurrent reinitialization survives.
- Do not leave an initialized engine undisposed. The executable check uses
  fixture/finally cleanup and always awaits `MySQLDatabase.close()`.
- Do not introduce an ORM or migration convention in a spec before the
  corresponding implementation exists.
- Do not commit inside a repository; the calling service owns atomic
  multi-repository work.
- Do not delete by all known IDs or by logical source when the contract is a
  submitted-table snapshot. Compute cleanup scope from submitted table IDs.
- Do not treat derived memory payload JSON as canonical meaning; rebuild it
  from the typed `content` document.
- Do not discard a candidate only because its content-addressed UID belongs to
  a `SUPERSEDED` or `EXPIRED` row. Reactivate the meaning as a new version UID;
  only `DELETED` content is a non-reactivating tombstone.
- Do not persist accepted Meta rows separately from their trusted long-term
  memories and relations.
- Do not place application memory tables in Meta or use a second
  engine/Session; qualify them to `memory.database` on the existing
  transaction.
- Keep source-query, replication, and DW MySQL sessions in UTC so `TIMESTAMP`
  values retain one absolute-time interpretation across backfill and CDC.
- Require the DW MySQL server to report `@@lower_case_table_names = 0`. The
  data-sync worker rejects case-insensitive modes at startup because tasks,
  schema locks, and ownership use binary target-table identities.
- Persist captured CDC rows and generation-reset deletes with bounded bulk
  statements; the surrounding service transaction owns their coordinates,
  task-state transitions, and ownership tombstones atomically.
- When saturated CDC events are drained during backfill, commit each event in
  its own bounded transaction and renew the task lease between transactions;
  do not run an independent heartbeat against a task row already locked by the
  current application transaction.
- A data-sync generation hash describes the physical table contract. Metric
  dependency metadata and the global schema fingerprint remain durable
  control-plane data but do not reset CDC coordinates or historical backfill.
