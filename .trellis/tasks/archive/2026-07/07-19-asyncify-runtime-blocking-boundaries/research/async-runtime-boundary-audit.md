# Research: Async Runtime Blocking Boundary Audit

- Query: Audit every synchronous production function/call reachable from FastAPI, arq, or LangGraph; classify convert/retain and propose the smallest async design and deterministic tests.
- Scope: mixed
- Date: 2026-07-19

## Findings

### Executive decision

Only two production boundaries need conversion:

1. `src/data_agent/ddl_metadata/parsing.py`: replace the public synchronous
   `parse_ddl()` with one public `async def parse_ddl(...)` that awaits
   `asyncio.to_thread(_parse_ddl_sync, ...)`. The private callable must contain
   the complete existing pipeline (byte check, SQLGlot parse, AST traversal,
   SQL regeneration, JSON projection, hashing, and Pydantic construction), not
   just `sqlglot.parse()`. This keeps all material parser CPU off the event-loop
   thread and leaves no public synchronous alternative.
2. `src/data_agent/logging.py` plus API/worker lifecycles: set `enqueue=True`
   on both enabled sinks and await `logger.complete()` after the final lifecycle
   record at API and worker shutdown. Put completion in a `finally` around the
   existing shutdown sequence so already-enqueued records still drain if a
   resource close raises; preserve the original close order and exception
   propagation.

Everything else should remain synchronous when it is startup-only object
construction, an in-memory accessor/constructor, bounded validation or
projection, or a pure deterministic transform. Actual database, Redis, HTTP,
embedding, LLM, Elasticsearch, Qdrant, arq, and checkpoint I/O is already
awaited.

### Why SQL parsing is material

- `DDLWorkflowNodes.parse_node()` is async but directly calls the synchronous
  parser (`src/data_agent/ddl_metadata/workflow/nodes.py:68-82`).
- The parser calls synchronous `sqlglot.parse()` and then walks/generates every
  AST before hashing the projections
  (`src/data_agent/ddl_metadata/parsing.py:163-181`,
  `src/data_agent/ddl_metadata/parsing.py:191-265`).
- Checked-in limits are 256 KiB, 50 tables, and 500 columns
  (`conf/app_config.yaml:42-44`).
- A read-only local measurement on this checkout (Python 3.13, SQLGlot
  28.10.1) used 50 tables/500 columns and 19,879 UTF-8 bytes: one parse took
  243.71 ms; five took 1,057.24 ms. This is machine-specific, but it establishes
  that valid bounded input can monopolize an event-loop thread for hundreds of
  milliseconds.
- The worker reaches the node through `await graph.ainvoke(...)`
  (`src/data_agent/ddl_metadata/worker/job_runner.py:368-372`), and the graph
  registers it as the first node
  (`src/data_agent/ddl_metadata/workflow/graph.py:26-39`).

Required error semantics: the private implementation must retain the existing
`DDLMetadataError` codes/stages and `ParseError` chaining
(`src/data_agent/ddl_metadata/parsing.py:169-232`). `asyncio.to_thread()`
propagates the result or exception to the awaiter. Cancellation of the awaiter
does not forcibly stop an already-running Python thread; the existing input
limits bound the residual work. Do not add shielding or catch
`CancelledError`.

### Why queued Loguru sinks are required

- `setup_logging()` currently removes all handlers and adds console/file sinks
  without `enqueue=True`; file directory creation and both `logger.add()` calls
  are at `src/data_agent/logging.py:118-151`.
- Loguru documents that `enqueue=True` makes the logging call non-blocking.
  Formatting, exception traceback projection, JSON serialization, terminal
  writes, file writes, rotation, and retention then run in the sink worker
  instead of the async caller.
- `logger.complete()` first waits for all queued records and returns an
  awaitable for coroutine-sink tasks. Await it after the final API record
  (`src/data_agent/application.py:46-58`) and final worker record
  (`src/data_agent/ddl_metadata/worker/lifecycle.py:83-99`).
- Keep `setup_logging()` synchronous. `Path.mkdir()` is startup-only
  (`src/data_agent/logging.py:140-143`), and invalid paths/levels/rotation must
  continue to fail startup as required by the logging spec.
- The formatter remains synchronous by design because with `enqueue=True` it
  executes on Loguru's consumer thread. Preserve `_json_formatter()`'s
  redacted exception rendering and strict JSON behavior
  (`src/data_agent/logging.py:65-109`).

### Complete synchronous-module inventory

The table covers every production module containing a synchronous function in
the current `src/data_agent` package.

| Module | Classification | Evidence and reason |
| --- | --- | --- |
| `application.py` | Retain `create_app`; add async log drain to lifespan | FastAPI/router/middleware assembly is startup-only (`application.py:103-118`); lifecycle I/O is awaited (`application.py:26-58`). |
| `main.py` | Retain | CLI entrypoint only calls Uvicorn startup (`main.py:11-20`); it is not executed inside an existing async runtime path. |
| `settings.py` | Retain | Validators are small deterministic checks (`settings.py:130-147`, `settings.py:262-276`); `from_yaml()` performs one startup/import-time file read (`settings.py:280-296`). |
| `logging.py` | **Convert sink delivery**, retain setup/formatters | Add queueing at sink registration (`logging.py:118-151`); formatter CPU and writes leave caller thread. Startup directory creation remains synchronous. |
| `ddl_metadata/api/jobs.py` | Retain | `_jobs()` only reads `request.app.state` (`api/jobs.py:17-20`); endpoint work awaits store operations (`api/jobs.py:27-72`). |
| `ddl_metadata/api/memories.py` | Retain | `_memories()` is the same in-memory dependency lookup (`api/memories.py:19-22`); service calls are awaited (`api/memories.py:28-93`). |
| `ddl_metadata/errors.py` | Retain | Exception construction copies bounded error metadata (`errors.py:7-28`), with no I/O. |
| `ddl_metadata/identifiers.py` | Retain | SHA-256/JSON identifiers and fingerprints are deterministic and bounded by the 50-table/500-column schema (`identifiers.py:9-73`). |
| `ddl_metadata/jobs/identifiers.py` | Retain | Question JSON/hash is bounded by the 100-question contract (`jobs/identifiers.py:10-25`; `models/semantic.py:68`). |
| `ddl_metadata/jobs/redis/base.py` | Retain | `awaitable()` only narrows the Redis client's awaitable type (`jobs/redis/base.py:13-22`); it does not execute I/O synchronously. |
| `ddl_metadata/jobs/redis/codec.py` | Retain | Redis record decode/JSON encode/Pydantic projection is bounded job-state work (`jobs/redis/codec.py:25-94`). |
| `ddl_metadata/jobs/redis/keys.py` | Retain | Pure key-string construction (`jobs/redis/keys.py:12-43`). |
| `ddl_metadata/jobs/redis/lease_store.py` | Retain | Constructor only binds references (`lease_store.py:19-22`); all Redis `eval`/`set`/release calls are awaited (`lease_store.py:24-63`). |
| `ddl_metadata/jobs/redis/outbox_store.py` | Retain | Constructor only binds references (`outbox_store.py:15-18`); bounded Redis reads and arq enqueue are awaited (`outbox_store.py:20-56`). |
| `ddl_metadata/jobs/redis/state_store.py` | Retain | Constructor only binds references (`state_store.py:26-29`); Redis calls are passed to `awaitable()` and awaited, while model conversion is local (`state_store.py:31-95`). |
| `ddl_metadata/jobs/store.py` | Retain, with oversize caveat | Constructors/properties/key helpers are in-memory (`jobs/store.py:38-67`); Redis work is awaited. Accepted DDL encoding is bounded, answer duplicate detection is at most 100 items (`jobs/store.py:69-87`, `jobs/store.py:157-184`). |
| `ddl_metadata/models/memory.py` | Retain | Pydantic after-validator checks a small tagged-union shape (`models/memory.py:92-108`). |
| `ddl_metadata/parsing.py` | **Convert public boundary** | Whole synchronous SQLGlot/AST/canonicalization/hash pipeline is material (`parsing.py:23-265`); only its private worker-thread implementation remains `def`. |
| `ddl_metadata/validation.py` | Retain | Set/list validation and stable-ID projection are pure and bounded by schema/model limits (`validation.py:19-240`). |
| `ddl_metadata/persistence/metadata_repository.py` | Retain | Constructor stores `AsyncSession` (`metadata_repository.py:25-27`); every SQLAlchemy execution is awaited (`metadata_repository.py:29-205`). |
| `ddl_metadata/memory/application/context.py` | Retain | `_choose_memory()` and nested `effective()` select/project bounded results (`context.py:51-72`, `context.py:198-205`); MySQL/search I/O is awaited (`context.py:78-160`). |
| `ddl_metadata/memory/application/service.py` | Retain | Constructor and `_normalize_content()` are dependency binding/type-specific Pydantic copies (`service.py:36-39`, `service.py:174-220`); persistence/search calls are awaited. |
| `ddl_metadata/memory/domain/candidates.py` | Retain | Candidate construction is pure and bounded by current schema/questions/metrics (`candidates.py:34-204`). |
| `ddl_metadata/memory/domain/payloads.py` | Retain | Canonical JSON, hashes, object-ID lists, and bounded search text are deterministic projections (`payloads.py:15-105`). |
| `ddl_metadata/memory/domain/ranking.py` | Retain | RRF is bounded by ES/Qdrant top-k and memory search limit (20 in config), and has no I/O (`ranking.py:6-28`; `conf/app_config.yaml:21,27,76`). |
| `ddl_metadata/memory/indexing/elasticsearch.py` | Retain | Constructor only stores async client/index name (`indexing/elasticsearch.py:12-15`); all index/search calls are awaited (`indexing/elasticsearch.py:17-105`). |
| `ddl_metadata/memory/indexing/qdrant.py` | Retain | UUID/filter helpers and constructor are pure (`indexing/qdrant.py:25-63`); client setup/upsert/delete/search are awaited (`indexing/qdrant.py:65-140`). |
| `ddl_metadata/memory/mysql/index_outbox.py` | Retain | Constructor only stores `AsyncSession` (`index_outbox.py:30-32`); SQL work is awaited and batches are configured to 100 (`index_outbox.py:34-202`; `conf/app_config.yaml:72`). |
| `ddl_metadata/memory/mysql/repository.py` | Retain | Row/Pydantic decoding and constructor are local (`repository.py:47-88`); every SQL operation is awaited and query result sizes are explicitly limited (`repository.py:90-631`). |
| `ddl_metadata/worker/job_runner.py` | Retain sync helpers | Logging field projection and checkpoint interrupt model conversion are small local work (`job_runner.py:56-145`); graph/Redis/checkpoint operations are awaited (`job_runner.py:147-432`). Queueing moves actual sink work off-thread. |
| `ddl_metadata/workflow/graph.py` | Retain | Graph builder only registers nodes/routes and compiles at worker startup (`workflow/graph.py:21-49`). |
| `ddl_metadata/workflow/llm_metadata_generator.py` | Retain constructor | Constructor binds or retrieves an already-constructed async model (`llm_metadata_generator.py:29-32`); all model calls use `ainvoke()` (`llm_metadata_generator.py:34-154`). |
| `ddl_metadata/workflow/nodes.py` | Retain small helpers/constructor; **await parser** | `_state_string`, error projection, and dependency binding are small (`workflow/nodes.py:39-66`); change only parser call at `workflow/nodes.py:68-86` to `await parse_ddl(...)`. Other model conversions/validation are bounded by persisted contracts. |
| `ddl_metadata/workflow/routing.py` | Retain | Seven functions perform constant-time state lookups and route selection only (`workflow/routing.py:8-69`). LangGraph intentionally accepts sync routing callables. |
| `infrastructure/checkpoint_store.py` | Retain getter | Getter is an initialized-client guard (`checkpoint_store.py:36-43`); initialization/setup/close are async and awaited (`checkpoint_store.py:16-50`). |
| `infrastructure/elasticsearch.py` | Retain constructor/getter | `AsyncElasticsearch(...)` only constructs transport state here (`infrastructure/elasticsearch.py:16-35`); actual calls and close are awaited by consumers (`infrastructure/elasticsearch.py:38-44`). |
| `infrastructure/llm_client.py` | Retain constructor/getter | Environment lookup and `ChatOpenAI` construction are startup-only (`llm_client.py:24-45`); capability probe uses `ainvoke` and close awaits the async HTTP client (`llm_client.py:48-66`). |
| `infrastructure/mysql.py` | Retain constructor/getter | `create_async_engine()` and sessionmaker construction do not connect; transaction I/O and dispose are awaited (`mysql.py:24-75`). |
| `infrastructure/qdrant.py` | Retain constructor/getter | Async client construction is startup-only (`qdrant.py:16-34`); all service operations and close are awaited (`qdrant.py:37-43`). |
| `infrastructure/redis.py` | Retain constructor/getter | `Redis.from_url()` creates a lazy async client (`redis.py:16-32`); operations and `aclose()` are awaited (`redis.py:35-40`). |
| `infrastructure/tei_embeddings.py` | Retain constructor/getter | `model_construct()` plus `AsyncInferenceClient` setup is startup-only object construction (`tei_embeddings.py:27-52`); embedding and close are awaited (`tei_embeddings.py:16-18`, `tei_embeddings.py:55-61`). |

Modules not listed above contain no synchronous function definitions. A
repository call search found no synchronous `requests`, blocking HTTP client,
socket, subprocess, `time.sleep`, database execute, Redis execute, embedding,
LLM invoke, or checkpoint call on an async hot path.

### Third-party constructor audit

Installed versions inspected in the active `uv` environment:

`sqlglot 28.10.1`, `loguru 0.7.3`, `redis 5.3.1`,
`elasticsearch 8.19.3`, `qdrant-client 1.18.0`, `SQLAlchemy 2.0.51`,
`langchain-openai 1.3.5`, `huggingface-hub 1.23.0`, `langgraph 1.2.9`,
and `arq 0.28.0`.

Inspection of the installed constructors used by the infrastructure managers
found configuration/transport object setup only: network I/O begins at the
subsequent awaited methods. This matches the repository's explicit first
network operations: ES/Qdrant index `setup()` is awaited at worker startup
(`worker/lifecycle.py:43-48`), the LLM capability probe is awaited
(`worker/lifecycle.py:60-62`), and checkpoint `__aenter__`/`asetup` are awaited
(`checkpoint_store.py:16-31`). Do not convert these constructors merely because
they produce async clients.

### Minimal implementation shape

```python
def _parse_ddl_sync(
    source: str,
    ddl: str,
    limits: APISettings,
) -> PhysicalSchema:
    # Entire current implementation.


async def parse_ddl(
    source: str,
    ddl: str,
    limits: APISettings = app_config.api,
) -> PhysicalSchema:
    return await asyncio.to_thread(_parse_ddl_sync, source, ddl, limits)
```

Then:

- change `parse_node()` to `schema = await parse_ddl(...)`;
- migrate every test call to `await parse_ddl(...)`;
- keep no `parse_ddl_sync` public alias and no dual sync/async test suite;
- add `enqueue=True` to both `logger.add()` calls;
- await `logger.complete()` in API and worker shutdown after the stopped record,
  with a `finally` guaranteeing drain of records emitted before a close error.

No executor abstraction, process pool, new config key, API response change,
job-name change, or persistence change is justified.

### Precise tests

1. **Parser behavior migration** (`tests/unit/ddl_metadata/test_parsing.py`):
   make the test and rejection helper async; await the public parser for all
   success/error/limit cases. Preserve exact schema equality, hashes, roles,
   `DDLMetadataError.code`, stage, and malformed SQL cause checks.
2. **Real off-thread boundary without sleeps**: monkeypatch
   `sqlglot.parse` with a function that records its thread ID, sets a
   `threading.Event` (`entered`), and blocks on a second event (`release`).
   Start `parse_ddl()` as a task; wait for `entered` via
   `await asyncio.to_thread(entered.wait)` guarded by `asyncio.wait_for`.
   Schedule `loop.call_soon(progress.set)` and await the `asyncio.Event`
   `progress`. Assert parser thread ID differs from the loop thread ID, then
   set `release` in `finally` and await the parser task. This proves event-loop
   progress through synchronization, not timing sleeps.
3. **Node contract** (`tests/unit/ddl_metadata/workflow/test_graph.py`):
   an async parser spy may verify `parse_node()` awaits the async contract and
   still converts `DDLMetadataError` to the same rejected state. Do not retain
   a sync compatibility fixture.
4. **All parser consumers**: migrate calls in
   `tests/unit/ddl_metadata/test_validation.py`,
   `tests/unit/ddl_metadata/memory/domain/test_memory.py`,
   `tests/integration/test_api.py`,
   `tests/integration/test_memory_services.py`,
   `tests/integration/persistence/test_metadata_repository.py`, and
   `tests/integration/persistence/test_memory_repository.py`. Sync unit tests
   that directly parse must become native async pytest tests, not use
   `asyncio.run()`.
5. **Queued logging determinism** (`tests/unit/infrastructure/test_logging.py`):
   make every file-reading logging test async; after emitting, `await
   logger.complete()` before reading. In `finally`, complete before
   `logger.remove()` so the temporary directory cannot disappear while the
   consumer thread still writes. Keep JSON/text, UTF-8, trace isolation,
   non-finite-number, and redacted exception assertions.
6. **Queue configuration**: monkeypatch/spy `logger.add` and assert every
   enabled console/file registration receives `enqueue=True`; also cover both
   sinks disabled.
7. **Shutdown flush and ordering**: patch resource closes and
   `logger.complete` with async spies. Drive the FastAPI lifespan and worker
   `shutdown()`, assert completion occurs after the final stopped log and all
   normal closes. Add a close-failure case proving completion still runs and
   the original close exception propagates.
8. **Stale-contract search**:
   `rg -n "parse_ddl\\(" src tests` must show only awaited public calls plus the
   private implementation definition; `rg -n "from .*parsing import parse_ddl"`
   must have no sync call sites. AST inspection should reject any public
   synchronous `parse_ddl` definition or compatibility alias.

Use `asyncio.wait_for()` only as a deadlock guard, never as the proof of
progress. Do not add `asyncio.sleep(<duration>)` polling.

### Files found

- `src/data_agent/ddl_metadata/parsing.py` — synchronous SQLGlot and complete
  physical-schema projection boundary.
- `src/data_agent/ddl_metadata/workflow/nodes.py` — async LangGraph node that
  currently calls the parser synchronously.
- `src/data_agent/logging.py` — Loguru formatting and sink registration.
- `src/data_agent/application.py` — FastAPI startup/shutdown lifecycle.
- `src/data_agent/ddl_metadata/worker/lifecycle.py` — arq worker
  startup/shutdown lifecycle.
- `src/data_agent/ddl_metadata/worker/job_runner.py` — arq-to-LangGraph runtime
  entry and retry/cleanup behavior.
- `src/data_agent/infrastructure/*.py` — async client constructors, actual I/O,
  and close paths.
- `src/data_agent/ddl_metadata/jobs/redis/*.py` — awaited Redis I/O plus bounded
  synchronous codecs/keys.
- `src/data_agent/ddl_metadata/memory/**` — awaited persistence/index calls and
  bounded domain projections.
- `tests/unit/ddl_metadata/test_parsing.py` — current synchronous public parser
  contract.
- `tests/unit/infrastructure/test_logging.py` — current immediate file-read
  assumptions that queueing invalidates.
- `pyproject.toml`, `uv.lock`, `conf/app_config.yaml` — dependency and runtime
  limits.

### External references

- Python 3.13 `asyncio.to_thread()`:
  https://docs.python.org/3.13/library/asyncio-task.html#asyncio.to_thread —
  runs a callable in a separate thread, propagates `contextvars`, and returns
  an awaitable result. The documentation notes the GIL caveat for CPU work;
  this design targets event-loop responsiveness, not parallel CPU throughput.
- Loguru 0.7.3 `logger.add()` and `logger.complete()`:
  https://loguru.readthedocs.io/en/stable/api/logger.html — `enqueue=True`
  makes logging calls non-blocking; `complete()` drains enqueued messages and
  returns an awaitable for asynchronous sink tasks.
- SQLGlot API: https://sqlglot.com/ — `parse()` synchronously returns syntax
  trees and SQLGlot is primarily Python code; it exposes no async parser API.

### Related specs

- `.trellis/spec/backend/index.md` — backend ownership and quality routing.
- `.trellis/spec/backend/quality-guidelines.md` — forbids sync client calls in
  the async infrastructure layer and defines native async test style.
- `.trellis/spec/backend/logging-guidelines.md` — sink ownership, formatting,
  redaction, file behavior, and required logging test matrix.
- `.trellis/spec/backend/external-service-integrations.md` — executable async
  client signatures and lifecycle contracts.
- `.trellis/spec/backend/error-handling.md` — propagation, retries, terminal
  state, and cleanup semantics.
- `.trellis/spec/backend/database-guidelines.md` — async session transaction
  and rollback/close behavior.
- `.trellis/spec/guides/cross-layer-thinking-guide.md` — trace signature and
  lifecycle changes through every consumer and test.

## Caveats / Not Found

- Trellis reported no session-active task, but the parent dispatch explicitly
  supplied `.trellis/tasks/07-19-asyncify-runtime-blocking-boundaries`; this
  research writes only to that directory.
- `DDLJobRequest.ddl` has `min_length` but no Pydantic `max_length`
  (`models/jobs.py:62-67`). The 256 KiB rejection happens later through
  `len(request.ddl.encode())` on the async endpoint thread
  (`jobs/store.py:69-77`). Accepted work is bounded, but an adversarially huge
  JSON string can incur request JSON/Pydantic processing and a full UTF-8
  encoding before rejection. No ASGI request-size middleware or proxy limit was
  found in the repository. This is a real residual boundary; changing it may
  alter HTTP validation/stage semantics and should be treated as an explicit
  hardening decision, not silently folded into the parser migration. A
  contract-preserving micro-hardening is to reject `len(ddl) > max_ddl_bytes`
  before encoding, then encode only the remaining bounded character count.
- `asyncio.to_thread()` does not terminate a running worker function when the
  awaiting task is cancelled. Current input limits cap the orphaned work, but
  hard preemption would require a process boundary and is out of scope.
- Queueing moves formatting and output off the caller thread but does not make
  an overloaded sink lossless without backpressure; Loguru's queue and
  shutdown drain semantics are the selected tradeoff.
- No synchronous database, Redis, HTTP, embedding, LLM, checkpoint, ES, or
  Qdrant operation was found on an async hot path.
- No `design.md` or `implement.md` existed when this research was performed.
