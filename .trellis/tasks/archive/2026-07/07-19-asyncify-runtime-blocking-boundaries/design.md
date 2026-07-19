# Async Runtime Boundary Design

## Design Goal

Keep FastAPI, arq, and LangGraph event loops responsive by moving the only
material synchronous runtime work off the loop, queueing log sink work, and
bounding oversized request rejection. Preserve every public transport,
persistence, queue, checkpoint, and error contract.

## Audited Boundary Decisions

The repository-wide inventory is recorded in
`research/async-runtime-boundary-audit.md`.

Convert:

- the public DDL parsing contract;
- Loguru console and file delivery;
- API and worker shutdown log draining;
- the unbounded UTF-8 size calculation before job acceptance.

Retain synchronous:

- startup-only YAML loading, directory creation, dependency construction,
  application/graph assembly, and guarded client getters;
- pure identifiers, Redis keys/codecs, routing, validation, row decoding,
  Pydantic projection, and memory-domain transforms;
- the private callable executed by `asyncio.to_thread()`.

All actual MySQL, Redis, Elasticsearch, Qdrant, TEI, LLM, arq, HTTP, and
checkpoint I/O already uses awaited async APIs.

## Async DDL Parsing Contract

`data_agent.ddl_metadata.parsing` exposes only:

```python
async def parse_ddl(
    source: str,
    ddl: str,
    limits: APISettings = app_config.api,
) -> PhysicalSchema
```

It awaits:

```python
asyncio.to_thread(_parse_ddl_sync, source, ddl, limits)
```

`_parse_ddl_sync()` owns the complete existing parsing pipeline, including
limit enforcement, SQLGlot parsing, AST traversal, canonical SQL generation,
hashing, and Pydantic model construction. This prevents part of the material
pipeline from leaking back onto the event loop.

`DDLWorkflowNodes.parse_node()` awaits the public parser. Every direct test
consumer becomes a native async pytest test. No public `parse_ddl_sync`,
compatibility alias, or duplicate sync test path remains.

`DDLMetadataError` codes, stages, safe details, and `ParseError` cause chaining
are unchanged. `asyncio.to_thread()` propagates results and exceptions.
Cancellation is not shielded; already-running thread work is bounded by input
limits and may finish after the awaiter is cancelled.

## Bounded Job-Ingress Size Check

Before UTF-8 encoding, `DDLJobStore.submit()` performs:

1. `len(request.ddl) > max_ddl_bytes` — immediate business rejection, because
   every Unicode code point occupies at least one UTF-8 byte;
2. otherwise encode the bounded string and compare its precise byte length.

This retains the existing `ddl_too_large` error contract for ASCII and
multibyte input while preventing unbounded encoding work. It deliberately does
not move the limit to Pydantic or ASGI middleware, which would change the
response projection and validation stage.

## Non-Blocking Logging and Shutdown

Both enabled Loguru sinks are registered with `enqueue=True`. Formatting,
JSON serialization, console/file writes, rotation, and retention run on the
Loguru queue consumer rather than the async caller.

`setup_logging()` remains synchronous because sink construction and
`Path.mkdir()` are startup-only and their errors must fail startup.

API and worker lifecycles emit the existing stopped event, then await
`logger.complete()`. The drain sits in a `finally` that runs even if an earlier
resource close fails; the original close exception propagates unchanged.
Existing reverse resource-close order remains intact.

Tests await `logger.complete()` before reading queued files and again before
removing handlers/temporary directories. They never rely on a fixed sleep.

## Compatibility

Unchanged:

- HTTP paths, status codes, request/response models, and business errors;
- arq job/cron registrations and Redis payload/key/Lua formats;
- MySQL schemas/statements and LangGraph state, topology, thread IDs, and
  checkpoints;
- configuration keys and logging event/field schemas;
- deterministic parser output, hashes, roles, limits, and rejection codes.

Changed internal contracts:

- `parse_ddl(...)` is await-only;
- production log delivery is queued;
- lifecycle shutdown includes explicit queue draining.

## Operational Trade-offs

- A thread pool improves event-loop responsiveness, not total CPU throughput.
  SQLGlot may remain GIL-bound.
- Cancelling an awaiting task cannot forcibly terminate a running worker
  thread. Existing DDL limits cap residual work.
- Loguru queueing favors non-blocking producers and deterministic shutdown
  drain; it does not add bounded backpressure or overload shedding.

## Rollback

The parser migration, ingress bound, and logging migration are separate
implementation checkpoints. If one fails, revert only that checkpoint and its
tests. No data, schema, Redis, checkpoint, or configuration migration requires
rollback.
