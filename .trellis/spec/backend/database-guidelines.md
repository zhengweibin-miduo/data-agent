# Database Guidelines

## Current Database Scope

MySQL is the only relational database currently wired into the application.
The project uses SQLAlchemy 2's async engine and `AsyncSession` with the
`asyncmy` driver. The DSN is validated by `MySQLSettings` in
`src/data_agent/settings.py`; engine and Session-factory lifecycle is owned by
`src/data_agent/infrastructure/mysql.py`.

The DDL metadata feature uses SQLAlchemy Core table definitions in
`src/data_agent/ddl_metadata/persistence/tables.py` plus Session-scoped
repositories. It deliberately does not add ORM entities or a migration
framework. `MetadataRepository` owns the four Meta snapshot tables;
`MemoryRepository` owns canonical long-term memory, typed relations, filters,
cursors, and lifecycle updates. The Meta tables use the default database in
`mysql.url`; both memory tables are schema-qualified to `memory.database`
(`data_agent` by default). They still share one engine and Session so MySQL can
commit the two InnoDB databases atomically.

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

## Scenario: Atomic Meta Snapshot and LLM Memory

### 1. Scope / Trigger

Use this contract when changing the four Meta tables, canonical memory,
memory relations, correction, payload rebuilding, or accepted-snapshot
persistence.

### 2. Signatures

```python
await MetadataSnapshotService.persist(snapshot, memory_candidates) -> SnapshotResult
await MemoryService.correct(memory_uid, content) -> CorrectionResult
await MemoryPayloadRebuilder.rebuild(after_id=None) -> PayloadRebuildResult
```

### 3. Contracts

`MetadataSnapshotService.persist()` is the only accepted-snapshot commit
boundary. In
one managed MySQL transaction spanning the default Meta database and the
configured application memory database it:

1. upserts submitted table, column, metric, and column/metric rows;
2. removes stale links and columns only for submitted table IDs;
3. removes metrics that became orphaned through that scoped cleanup;
4. upserts accepted canonical memories and typed relations; and
5. archives older active decisions superseded by accepted replacements.

Stable SHA-256 IDs and the unique memory-relation triple make re-execution safe
after a worker crash. Tables absent from the submitted DDL are outside cleanup
scope. No repository write is permitted while a graph waits for user input or
before deterministic validation succeeds.

Memory corrections use a separate managed transaction: validate current Meta
references, append the user-confirmed replacement, add `SUPERSEDES`, and
archive the replaced active memory atomically. They do not patch the accepted
Meta snapshot; the source must pass through the DDL workflow again.

Derived payload rebuilding reads at most `memory.rebuild_batch_size` rows after
an explicit numeric cursor. `PayloadRebuildResult.next_after_id` lets the
caller advance through every batch; per-row savepoints isolate corrupt
canonical content without rolling back successful rows in the same batch.
Callers restart from an earlier cursor when they intentionally want to retry a
failed row. Trust provenance lives in canonical content, so a missing or
corrupt old payload can be rebuilt without consulting that payload.

Exact metric-memory reuse batch-loads outgoing `REFERENCE` targets from active
metric definitions and includes only the referenced question/answer audit
records. Immutable historical answers may remain `NORMAL`, but an unrelated
audit record must not become current graph context merely because its schema
fingerprint matches.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Graph is waiting or validation is incomplete | Write no Meta or trusted memory rows |
| Snapshot statement fails | Roll back all Meta, memory, relation, and archive changes |
| Schema-qualified memory statement fails after Meta writes | Roll back the earlier Meta writes in the same transaction |
| Same accepted snapshot is replayed | Stable IDs and unique relations produce the same state |
| Submitted table drops a column | Remove stale rows only inside submitted table IDs |
| Correction repeats identical user-confirmed content | Return `unchanged_correction`; do not self-supersede |
| Payload is missing, stale, or corrupt | Rebuild from canonical content; never infer trust from payload |
| One rebuild row is corrupt | Roll back that row to its savepoint and continue the bounded batch |

### 5. Good / Base / Bad Cases

- Good: accepted Meta, canonical memories, typed relations, and supersession
  commit together after deterministic validation.
- Base: browser correction appends trusted memory and requires a later DDL run
  before Meta changes.
- Bad: repository methods commit independently, or correction edits current
  Meta rows without full DDL validation.

### 6. Tests Required

```powershell
uv run pytest tests/integration/persistence
uv run pytest tests/integration/test_memory_services.py
uv run pytest tests/integration/test_ddl_metadata_flow.py
```

Tests must assert full rollback, unrelated-table preservation, repeat
idempotency, correction supersession/no-op behavior, canonical trust rebuild,
relation-aware reuse, and post-commit replay safety.

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
`llm_memory` and `llm_memory_relation`. SQLAlchemy Core definitions must remain
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
- Ownership: `meta.sql` owns exactly the four Meta tables.
  `data_agent.sql` is idempotent, uses InnoDB, and owns exactly the two
  application memory tables.
- Existing volume: entrypoint scripts do not rerun. Apply `data_agent.sql`
  explicitly through the local root account; never drop or migrate legacy
  `meta.llm_memory*` objects without approval.

### 4. Validation & Error Matrix

| Condition | Expected result |
|-----------|-----------------|
| Empty `mysql_data` | Execute `data_agent.sql`, `dw.sql`, then `meta.sql` |
| Initialized `mysql_data` | Skip bootstrap scripts and preserve data |
| Missing init-directory mount | Start MySQL without creating the three local databases |
| `GRANT` user differs from `MYSQL_USER` and does not exist | Initialization fails at `GRANT` |
| Compose cannot resolve `./mysql` | `docker compose config` or startup reports the invalid mount |
| Memory database equals the `mysql.url` default | Configuration validation fails before startup |

### 5. Good / Base / Bad Cases

- Good: an empty disposable volume creates `data_agent`, `dw`, and `meta`; the
  application user can access each owned schema while Meta contains only its
  four business tables.
- Base: restarting an initialized local container leaves all database contents
  unchanged.
- Bad: forcing the scripts to run on every startup can execute their
  `DROP TABLE` statements and destroy local data.

### 6. Tests Required

- Run `docker compose -f docs/docker/docker-compose.yml config` and assert that
  both `/var/lib/mysql` and the read-only `/docker-entrypoint-initdb.d` mount
  are present.
- Search all files under `docs/docker/mysql/` and assert that bootstrap grants
  target `'data_agent'@'%'` and do not reference stale users.
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
