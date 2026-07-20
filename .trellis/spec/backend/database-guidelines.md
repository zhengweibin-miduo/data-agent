# Database Guidelines

## Current Database Scope

MySQL is the only relational database currently wired into the application.
The project uses SQLAlchemy 2's async engine and `AsyncSession` with the
`asyncmy` driver. The DSN is validated by `MySQLSettings` in
`src/data_agent/settings.py`; engine and Session-factory lifecycle is owned by
`src/data_agent/infrastructure/mysql.py`.

The DDL metadata feature uses SQLAlchemy Core table definitions in
`ddl_metadata/persistence/tables.py` for Meta snapshots and
`ddl_metadata/memory/mysql/tables.py` for long-term memory. Both import the
single `MetaData` owner from `ddl_metadata/persistence/schema.py`. The project
deliberately does not add ORM entities or a migration framework.
`MetadataRepository` owns the four Meta snapshot tables; `MemoryRepository`
owns authoritative records, append-only events, typed links, and browser
mutations; `MemoryIndexOutboxRepository` owns derived-index desired state,
claiming, retry, acknowledgement, projections, and rebuild scans. The Meta
tables use the default database in `mysql.url`; all four memory tables are
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
- MySQL upserts use `sqlalchemy.dialects.mysql.insert()` with explicit update
  columns.
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
4. upserts accepted authoritative memories, ADD history, and typed links; and
5. writes Elasticsearch and Qdrant desired operations to `memory_index_outbox`.

Stable SHA-256 IDs and the unique memory-link triple make re-execution safe
after a worker crash. Replaying an accepted snapshot must not reactivate a UID
that a user already soft-deleted; its DELETE projection remains the desired
outbox state. Tables absent from the submitted DDL are outside cleanup scope.
No repository write is permitted while a graph waits for user input or before
deterministic validation succeeds.

Memory updates use a separate managed transaction: validate kind, scope, and
current Meta references, then append a user-confirmed UPDATE event. They do not
patch active authoritative content or Meta; the source must pass through the
DDL workflow again. DELETE is an audited soft delete plus two DELETE outbox rows.

Derived-index rebuilding reads at most `memory.rebuild_batch_size` ACTIVE rows
after an explicit numeric cursor and writes two UPSERT outbox rows per UID.
Rebuild explicitly recreates only the configured project ES index and Qdrant
collection; MySQL content is never reconstructed from an index payload.

Exact metric-memory reuse batch-loads outgoing `DERIVED_FROM` targets from active
metric definitions and includes only the referenced question/answer audit
records. Immutable historical answers may remain `NORMAL`, but an unrelated
audit record must not become current graph context merely because its schema
fingerprint matches.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Graph is waiting or validation is incomplete | Write no Meta or trusted memory rows |
| Snapshot statement fails | Roll back all Meta, memory, event, link, and outbox changes |
| Schema-qualified memory statement fails after Meta writes | Roll back the earlier Meta writes in the same transaction |
| Same accepted snapshot is replayed | Stable IDs and unique relations produce the same state |
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
deletion, and post-commit replay safety.

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

## Schema and Migrations

No migration tool or migration directory exists. Local MySQL bootstrap
initializes the `dw`, `meta`, and application `data_agent` databases from
`docs/docker/mysql/`. `meta.sql` contains only `table_info`, `column_info`,
`metric_info`, and `column_metric`; `data_agent.sql` idempotently owns
`agent_memory`, `agent_memory_event`, `agent_memory_link`, and
`memory_index_outbox`. SQLAlchemy Core definitions must remain
compatible with those bootstrap schemas. Integration fixtures may call
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

The current script order is lexical: `data_agent.sql`, `dw.sql`, then
`meta.sql`.

### 3. Contracts

- Host source: `docs/docker/mysql/` relative to the Compose file.
- Container target: `/docker-entrypoint-initdb.d`, mounted read-only.
- Execution boundary: the official `mysql:8.4` entrypoint processes the scripts
  only when `/var/lib/mysql` is uninitialized.
- Persistence: normal restarts reuse `mysql_data` and do not rerun the scripts.
- Identity: `MYSQL_USER=data_agent`; every bootstrap `GRANT` must target
  `'data_agent'@'%'` unless Compose is changed in the same task.
- Databases: the bootstrap scripts create `data_agent`, `dw`, and `meta`;
  Compose does not set `MYSQL_DATABASE`.
- Documentation: every bootstrap `CREATE TABLE` has a Chinese table-level
  `COMMENT`, and every business column has a Chinese column-level `COMMENT`.
  Comments explain business meaning without changing types, constraints,
  indexes, foreign keys, seed data, or statement order.
- Ownership: `meta.sql` owns exactly the four Meta tables.
  `data_agent.sql` uses InnoDB and owns exactly the four application memory
  tables and does not manage retired memory contracts.
- Existing volume: entrypoint scripts do not rerun. Apply `data_agent.sql`
  explicitly through the local root account when the current application
  memory tables are missing; never touch Meta tables.

### 4. Validation & Error Matrix

| Condition | Expected result |
|-----------|-----------------|
| Empty `mysql_data` | Execute `data_agent.sql`, `dw.sql`, then `meta.sql` |
| Initialized `mysql_data` | Skip bootstrap scripts and preserve data |
| Missing init-directory mount | Start MySQL without creating the three local databases |
| `GRANT` user differs from `MYSQL_USER` and does not exist | Initialization fails at `GRANT` |
| A table or business column lacks a Chinese `COMMENT` | Static bootstrap review fails before merge |
| Compose cannot resolve `./mysql` | `docker compose config` or startup reports the invalid mount |
| Memory database equals the `mysql.url` default | Configuration validation fails before startup |

### 5. Good / Base / Bad Cases

- Good: an empty disposable volume creates `data_agent`, `dw`, and `meta`; the
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
- Search all files under `docs/docker/mysql/` and assert that bootstrap grants
  target `'data_agent'@'%'` and do not reference stale users.
- Statically assert that every bootstrap table and business column carries a
  non-empty Chinese `COMMENT`; compare SQL tokens with comments removed when a
  comment-only task must prove that no schema or seed-data behavior changed.
- Run the repository checks against the default Meta URL after applying
  `data_agent.sql`; assert the SQLAlchemy memory objects use
  `schema=memory.database` and a forced memory-side constraint failure rolls
  back preceding Meta writes.
- When Docker is available and initialization behavior changes, use a
  disposable project/volume to assert that `data_agent`, `dw`, and `meta`
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

`docs/docker/mysql/data_agent.sql` owns fresh bootstrap, while
`upgrade_mem0_long_term_memory.sql` is the explicit one-time non-destructive
upgrade for an initialized database. The projection version must be bumped and
the project-owned ES index and Qdrant collection explicitly recreated after
the MySQL upgrade. Rebuild advances each scanned ACTIVE authority row to the
configured projection version while enqueueing both targets.

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
- Do not persist accepted Meta rows separately from their trusted long-term
  memories and relations.
- Do not place application memory tables in Meta or use a second
  engine/Session; qualify them to `memory.database` on the existing
  transaction.
