# Error Handling

## Current Strategy

The repository has no custom exception hierarchy and no API response layer.
Errors from configuration parsing and third-party async clients normally
propagate to the caller unchanged. Local code adds an explicit `RuntimeError`
only for a lifecycle misuse that it can identify precisely.

## Client Lifecycle Errors

Every manager's `get_client()` rejects access before initialization. For
example, `app/client/mysql_client_manager.py` uses this shape:

```python
if cls._client is None:
    raise RuntimeError(
        "MySQL 客户端尚未初始化，请先调用 MysqlClientManager.initialize()"
    )
```

`QdrantClientManager`, `ElasticsearchClientManager`, and
`TeiEmbeddingClientManager` follow the same pattern with service-specific
messages. Keep the message actionable and preserve the concrete manager name.

Closing an uninitialized or already closed manager is deliberately harmless.
Most managers use this shape:

```python
if cls._client is None:
    return
```

MySQL additionally clears its engine and Session-factory references before
awaiting disposal so a concurrent replacement is not erased. In every manager,
a later `initialize()` creates a fresh resource after close.

## Propagation and Cleanup

- `AppConfigModel.from_yaml()` lets file, YAML, and Pydantic validation errors
  propagate. `ConfigModel` uses `extra="forbid"`, so unknown keys are errors
  rather than silently ignored values.
- Client initialization and request failures are not wrapped in generic
  exceptions. The original SQLAlchemy, Elasticsearch, Qdrant, Hugging Face, or
  transport exception remains available to the caller.
- `MysqlClientManager.session()` commits on normal exit; on any
  `BaseException`, it rolls back and re-raises the same exception. Session
  closure is owned by the async context manager.
- Live checks acquire the managed client and close it in `finally`; see both
  files under `app_test/client/`. Keep cleanup independent of assertion or
  request success.

## Metadata CLI Failures

`app/script/sync_metadata.py` is a process boundary. It does not convert
failures into success values: `argparse`, file, YAML, Pydantic, SQLAlchemy,
Qdrant, Elasticsearch, and Hugging Face errors reach `main()` and therefore
produce a nonzero process exit.

Configuration is parsed before external clients are initialized. After a valid
configuration is loaded, client initialization and synchronization run inside a
`try/finally` that always awaits all four close operations:

```python
close_results = await asyncio.gather(
    MysqlClientManager.close(),
    QdrantClientManager.close(),
    ElasticsearchClientManager.close(),
    TeiEmbeddingClientManager.close(),
    return_exceptions=True,
)
```

Closing an uninitialized manager is safe, so a failure during the second or
third initialization still attempts every close. Failure precedence is:

1. If initialization or synchronization already failed, preserve that primary
   exception and add every close failure through `BaseException.add_note()`.
2. If business work succeeded but one or more closes failed, raise a
   `BaseExceptionGroup` containing every close failure.
3. Never let the first close failure prevent the remaining close operations
   from completing.

The MySQL Session rolls back when any later Qdrant, Elasticsearch, or TEI step
fails. Already completed external upserts may remain because there is no
cross-storage transaction. Do not hide this partial state or add automatic
deletion; repair the dependency and replay the stable-ID upserts.

## API Error Responses

There is no web framework, route handler, or serialized error response format
in this repository. Do not invent status-code or JSON-envelope rules. Add this
section only when an API boundary is implemented.

## Common Mistakes

- Do not return `None` from `get_client()` when initialization was forgotten;
  fail with the established `RuntimeError`.
- Do not swallow configuration or transport exceptions with a broad
  `except Exception`.
- Do not swallow an exception raised inside a managed MySQL Session; rollback
  and preserve the original failure.
- Do not skip async cleanup after a live integration assertion fails.
- Do not use a failing sequential close loop or `asyncio.gather()` without
  `return_exceptions=True`; either can leave later managers unclosed.
- Do not replace a synchronization failure with a cleanup failure. Preserve the
  primary exception and attach cleanup context.
- Do not claim multi-storage rollback: MySQL can roll back while completed
  Qdrant or Elasticsearch upserts remain.
- Do not add connection side effects to package `__init__.py` files; lifecycle
  remains explicit through manager methods.
