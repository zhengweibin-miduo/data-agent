# Database Guidelines

## Current Database Scope

MySQL is the only relational database currently wired into the application.
The project uses SQLAlchemy 2's async engine with the `asyncmy` driver. The
dependency declarations are in `pyproject.toml`, the DSN is validated by
`MysqlConfig` in `app/conf/app_config.py`, and engine lifecycle is owned by
`app/client/mysql_client_manager.py`.

The repository does not yet contain ORM models, `AsyncSession` factories,
repositories, schema migrations, or application queries. Do not describe or
assume conventions for those absent layers.

## Engine Lifecycle

Follow the existing manager pattern instead of constructing engines at each
call site:

```python
class MysqlClientManager:
    _client: ClassVar[AsyncEngine | None] = None

    @classmethod
    def initialize(cls) -> AsyncEngine:
        if cls._client is None:
            cls._client = create_async_engine(app_config.mysql.url)
        return cls._client
```

`initialize()` is idempotent, `get_client()` requires prior initialization,
and `close()` awaits `AsyncEngine.dispose()` before resetting the class state.
This is demonstrated by both `app/client/mysql_client_manager.py` and
`app_test/client/test_mysql_client_manager.py`.

## Query Pattern in the Repository

The only established query is the live health check:

```python
async with client.connect() as connection:
    assert await connection.scalar(text("SELECT 1")) == 1
```

This example establishes async connection use and SQLAlchemy's `text()` for a
literal probe. It does not establish a general raw-SQL, repository, transaction,
or session policy. Define those contracts when the first production persistence
feature is introduced.

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
- Do not leave an initialized engine undisposed. The MySQL live check uses
  `try/finally` and always awaits `MysqlClientManager.close()`.
- Do not introduce an ORM or migration convention in a spec before the
  corresponding implementation exists.
