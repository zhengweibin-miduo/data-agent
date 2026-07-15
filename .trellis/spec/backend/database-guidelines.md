# Database Guidelines

## Current Database Scope

MySQL is the only relational database currently wired into the application.
The project uses SQLAlchemy 2's async engine and `AsyncSession` with the
`asyncmy` driver. The DSN is validated by `MysqlConfig` in
`app/conf/app_config.py`; engine and Session-factory lifecycle is owned by
`app/client/mysql_client_manager.py`.

The repository does not yet contain ORM models, repositories, schema
migrations, or production persistence queries. Do not describe or assume
conventions for those absent layers.

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

## Query Pattern in the Repository

The executable checks use SQLAlchemy `text()` for a health probe and a
short-lived persistent transaction table that is explicitly dropped:

```python
async with client.connect() as connection:
    assert await connection.scalar(text("SELECT 1")) == 1
```

These examples validate connection health plus commit and rollback behavior.
They do not establish a production raw-SQL or repository policy. Define those
contracts when the first persistence feature is introduced.

## Schema and Migrations

No migration tool or migration directory exists. There is therefore no current
convention for table names, column names, indexes, migration identifiers, or
upgrade/downgrade behavior. MySQL bootstrap currently creates only the
`data_agent` database through `docs/docker/docker-compose.yml` and the CI MySQL
service in `.github/workflows/ci.yml`.

## Configuration and Naming

- The YAML key is `mysql.url` in `conf/app_config.yaml`.
- The URL uses `mysql+asyncmy://` and is typed as `str` by `MysqlConfig`.
- Local Compose and CI consistently use database/user name `data_agent`.
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
- Do not introduce an ORM or migration convention in a spec before the
  corresponding implementation exists.
