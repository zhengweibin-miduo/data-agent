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
- Do not add connection side effects to package `__init__.py` files; lifecycle
  remains explicit through manager methods.
