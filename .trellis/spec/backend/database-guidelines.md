# Database Guidelines

## Current Database Scope

MySQL is the only relational database currently wired into the application.
The project uses SQLAlchemy 2's async engine and `AsyncSession` with the
`asyncmy` driver. The DSN is validated by `MysqlConfig` in
`app/conf/app_config.py`; engine and Session-factory lifecycle is owned by
`app/client/mysql_client_manager.py`.

The repository now has one concrete persistence boundary:
`app/repository/metadata_repository.py`. It uses qualified SQL text for dynamic
DW reads and the SQLAlchemy mappings under `app/model/` for fixed Meta MySQL
upserts. There is still no schema migration or automatic table-creation path,
so do not generalize this feature-specific Repository into a migration or
generic ORM convention.

## Engine Lifecycle

Follow the existing manager pattern instead of constructing engines at each
call site:

```python
class MysqlClientManager:
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
async with MysqlClientManager.session() as session:
    await session.execute(statement)
```

Each context receives a fresh Session bound to the shared engine. Normal exit
commits; exceptional exit rolls back and re-raises the original exception;
the Session closes on both paths. The factory uses `expire_on_commit=False`.

## Query Patterns in the Repository

The executable checks use SQLAlchemy `text()` for a health probe and a
short-lived persistent transaction table that is explicitly dropped:

```python
async with client.connect() as connection:
    assert await connection.scalar(text("SELECT 1")) == 1
```

These examples validate connection health plus commit and rollback behavior.
The metadata Repository is the first persistence feature and establishes the
narrow cross-schema contract below; it does not establish raw SQL or ORM as the
default for every future feature.

## Metadata Synchronization SQL Boundary

`app/repository/metadata_repository.py` is the first concrete persistence
Repository. Its policy is intentionally limited to the metadata CLI:

```python
async with MysqlClientManager.session() as session:
    repository = MetadataRepository(session, qdrant, elasticsearch)
    await MetadataSyncService(repository, embeddings).sync(config)
```

- Reuse one managed Session and qualified `dw.<table>` / `meta.<table>` names;
  do not add a second DSN for `dw`.
- Read the actual DW shape from `information_schema.columns` and validate every
  configured table and column before a distinct-value query or storage write.
- Obtain mapping rows from the executed `Result`, for example
  `(await session.execute(statement, params)).mappings()`.
  `AsyncSession` has no `mappings()` method; do not reverse this call chain.
- Dynamic identifiers must match `^[A-Za-z_][A-Za-z0-9_]*$`, belong to the
  validated schema, and be quoted. Data values and limits remain bound
  parameters.
- Read at most 10 distinct non-null example values per configured field. Only
  `sync: true` fields receive the separate 100,000-value read.
- Service converts validated configuration into the business dataclasses under
  `app/entity/`; Repository write signatures accept those Entities, not config
  models or untyped dictionaries.
- Map `meta.table_info`, `meta.column_info`, `meta.metric_info`, and
  `meta.column_metric` with the four classes under `app/model/`. Their schema,
  lengths, SQL types, nullable flags, and composite primary key must match
  `docs/docker/mysql/meta.sql` exactly.
- Build MySQL statements with
  `sqlalchemy.dialects.mysql.insert(Model).on_duplicate_key_update(...)` and
  execute them with `dataclasses.asdict()` parameter mappings. Keep JSON fields
  as Python lists so SQLAlchemy's `JSON` type serializes them exactly once;
  pre-serializing with `json.dumps()` stores a JSON string instead of an array.
- Stable relational identities are the table name, `<table>.<column>`, metric
  name, and `(column_id, metric_id)`. Never delete rows merely because the
  current YAML omits them.
- The Session commits only after the whole synchronization succeeds and rolls
  back on any propagated failure. Completed external upserts may remain; the
  replay contract is defined in
  [External Service Integrations](./external-service-integrations.md#scenario-replayable-metadata-synchronization).

## Schema and Migrations

No migration tool or migration directory exists. The ORM Models describe the
existing `meta.sql` schema but never call `create_all()` and do not own DDL.
Local MySQL bootstrap initializes the `dw` and `meta` sample databases from
`docs/docker/mysql/`; it does not create a `data_agent` database. CI creates
only `meta` through the MySQL service in `.github/workflows/ci.yml`.

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

The current script order is lexical: `dw.sql`, then `meta.sql`.

### 3. Contracts

- Host source: `docs/docker/mysql/` relative to the Compose file.
- Container target: `/docker-entrypoint-initdb.d`, mounted read-only.
- Execution boundary: the official `mysql:8.4` entrypoint processes the scripts
  only when `/var/lib/mysql` is uninitialized.
- Persistence: normal restarts reuse `mysql_data` and do not rerun the scripts.
- Identity: `MYSQL_USER=data_agent`; every bootstrap `GRANT` must target
  `'data_agent'@'%'` unless Compose is changed in the same task.
- Databases: the bootstrap scripts create only `dw` and `meta`; Compose does
  not set `MYSQL_DATABASE`.

### 4. Validation & Error Matrix

| Condition | Expected result |
|-----------|-----------------|
| Empty `mysql_data` | Execute `dw.sql`, then `meta.sql` |
| Initialized `mysql_data` | Skip bootstrap scripts and preserve data |
| Missing init-directory mount | Start MySQL without creating `dw` or `meta` |
| `GRANT` user differs from `MYSQL_USER` and does not exist | Initialization fails at `GRANT` |
| Compose cannot resolve `./mysql` | `docker compose config` or startup reports the invalid mount |

### 5. Good / Base / Bad Cases

- Good: an empty disposable volume creates `dw` and `meta`, and the
  `data_agent` user can access both sample databases.
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
- When Docker is available and initialization behavior changes, use a
  disposable project/volume to assert that `dw` and `meta` exist after the
  first healthy startup. Never delete the developer's shared `mysql_data`
  volume for this check.

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
- The URL uses `mysql+asyncmy://` and is typed as `str` by `MysqlConfig`.
- Local Compose and CI use `data_agent` as the application user and `meta` as
  the application's default database.
- Do not duplicate the DSN in client modules; read it from the shared
  `app_config` instance.

## Common Mistakes

- Do not use SQLAlchemy's synchronous engine in async application code.
- Do not create an engine per query; reuse `MysqlClientManager`.
- Do not share one `AsyncSession` across concurrent tasks; enter a new
  `MysqlClientManager.session()` context per transaction.
- Do not clear shared manager state after awaiting disposal; detach the old
  state first so concurrent reinitialization survives.
- Do not leave an initialized engine undisposed. The executable check uses
  `try/finally` and always awaits `MysqlClientManager.close()`.
- Do not use `Session.add_all()` or `merge()` for replayable metadata writes;
  they do not provide the required batch `ON DUPLICATE KEY UPDATE` contract.
- Do not call `json.dumps()` for ORM `JSON` columns; pass the Entity list value.
- Do not add `ForeignKey`, `relationship`, synthetic IDs, timestamps, or
  `create_all()` unless the database DDL and task explicitly require them.
