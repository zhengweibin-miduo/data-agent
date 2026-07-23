# Runtime Assembly Design

## Context

The FastAPI and arq entry points currently repeat shared resource initialization
and shutdown while also composing role-specific objects. The target is one deep
runtime assembly module with a small external interface and a private,
extensible resource plan.

## Module and seam

Add `src/data_agent/runtime.py` as the owner of process resource plans,
initialization order, state publication, rollback, normal shutdown, and
lifecycle logging.

Its external interface is:

```python
class RuntimeRole(StrEnum):
    API = "api"
    DDL_METADATA_WORKER = "ddl_metadata_worker"

async def start(
    role: RuntimeRole,
    target: State | MutableMapping[str, Any],
) -> RuntimeHandle: ...

async def stop(handle: RuntimeHandle) -> None: ...
```

`RuntimeHandle` records the role, completed private actions, and closed state.
It does not expose infrastructure clients or allow callers to register actions.

The seam has two real target adapters: FastAPI/Starlette `State` and the arq
mutable context mapping. Publication differences remain private to the module.

## Private resource plan

A private action describes:

- a stable action name for diagnostics;
- async initialization against private runtime context;
- optional async close behavior;
- whether the action completed and therefore belongs in rollback/shutdown.

Plans are ordered and selected by `RuntimeRole`.

Shared plan:

1. configure logging;
2. initialize Redis;
3. initialize MySQL;
4. initialize Elasticsearch;
5. initialize Qdrant;
6. initialize TEI.

API continuation:

1. construct `DDLJobStore`;
2. publish `jobs`;
3. construct and publish `MemoryService`;
4. construct and publish `ConversationService`;
5. emit the existing API started event.

Worker continuation:

1. set up Elasticsearch and Qdrant memory indexes independently; preserve the
   existing deferred-warning behavior for each failed target;
2. initialize LLM and perform the structured-output capability check;
3. initialize CheckpointStore;
4. construct and publish `jobs`, `conversation_extractor`, and `graph`;
5. run `dispatch_pending(ctx)` and `cleanup_checkpoints(ctx)`;
6. emit the existing Worker started event.

Role-specific object construction and maintenance actions have no close
callback. Resource actions close the exact existing class-managed resource.
The logging action drains Loguru last.

## Entry-point integration

FastAPI lifespan:

```python
handle = await start(RuntimeRole.API, app.state)
try:
    yield
finally:
    await stop(handle)
```

Worker:

```python
ctx["_runtime_handle"] = await start(RuntimeRole.DDL_METADATA_WORKER, ctx)
```

Shutdown removes and validates `_runtime_handle`, then calls `stop(handle)`.
Existing public/business state keys and availability timing remain unchanged.

## Failure behavior

### Startup

An action is added to the handle only after successful initialization. If a
later action fails:

1. close every completed closeable action in reverse order;
2. continue after rollback close failures;
3. emit a safe structured rollback-failure event containing role, action name,
   and `error_type`, never exception text or connection details;
4. always drain Loguru if logging setup completed;
5. re-raise the original startup exception unchanged.

### Normal shutdown

`stop()` rejects an invalid or already closed handle. It marks the handle closed
before awaiting resource closes so concurrent/repeated stop calls cannot run the
same plan twice.

Every closeable action is attempted in reverse order. The existing stopped event
is emitted after infrastructure closes and before Loguru drain. One close error
is re-raised unchanged, including `CancelledError`; multiple ordinary close
errors become `ExceptionGroup` in close order, while a set containing a direct
`BaseException` becomes `BaseExceptionGroup`. `logger.complete()` is always
awaited. State keys published by this startup are restored before resources are
closed, including restoration of any value that existed before startup.

## Compatibility

Preserve:

- FastAPI lifespan and arq callback names;
- `app.state.jobs`, `app.state.memories`, `app.state.conversations`;
- `ctx["jobs"]`, `ctx["conversation_extractor"]`, `ctx["graph"]`;
- worker index deferred behavior;
- LLM capability, CheckpointStore, graph dependency, dispatch, and cleanup order;
- existing lifecycle event names, fields, and Chinese messages;
- all HTTP, Redis, MySQL, LangGraph, and configuration contracts.

The internal `_runtime_handle` key is new and reserved for Worker lifecycle
coordination.

## Testing

Test only through the public runtime interface and observable entry points:

- normal API and Worker startup/state publication;
- existing normal reverse close order;
- startup failure at multiple points and reverse rollback of completed actions;
- rollback close failure does not replace the startup exception;
- shutdown continues after one close error;
- one shutdown error preserves object identity;
- multiple shutdown errors produce `ExceptionGroup`;
- Loguru draining occurs on normal stop, rollback, and close failure;
- repeated or invalid handle stop raises actionable `RuntimeError`.

Replace lifecycle tests that encode the old first-error-stops-cleanup behavior;
do not layer contradictory tests.

## Rollback

The change is a hard internal migration. Rollback consists of restoring lifecycle
code in `application.py` and `ddl_metadata/worker/lifecycle.py`, removing
`runtime.py`, and restoring the prior lifecycle tests/spec text. No stored data,
configuration, or external protocol migration is involved.
