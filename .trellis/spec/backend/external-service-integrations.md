# External Service Integrations

## Every Client Declares Its Own Timeouts

No infrastructure client may rely on library defaults for timeouts. A client
without a read timeout turns a half-open TCP connection or an unresponsive
service into a call that never returns, and a business exception handler cannot
catch a call that does not return: the API request hangs, or a worker cron slot
is occupied forever. Timeout values come from `conf/app_config.yaml` so they are
reviewable and environment-specific.

Current injection points — verify parameter names against the locked dependency
versions before changing them, because these libraries rename timeout arguments
across major versions:

| Client | Parameters |
|---|---|
| `Redis.from_url` | `socket_timeout`, `socket_connect_timeout`, `health_check_interval` |
| `AsyncElasticsearch` | `request_timeout`, `max_retries`, `retry_on_timeout` |
| `AsyncQdrantClient` | `timeout` |
| `AsyncInferenceClient` (TEI) | `timeout` |
| `ChatOpenAI` | `timeout`, `max_retries` |

### Redis read timeout is coupled to the SSE heartbeat

The job event stream uses `xread(block=api.sse_heartbeat_seconds * 1000)`, so
blocking for a full heartbeat interval is **normal idle behavior**, not a fault.
`socket_timeout` is redis-py's per-command read timeout, so any value less than
or equal to the heartbeat turns every idle heartbeat into a `TimeoutError` and
breaks the stream. `AppSettings` enforces
`redis.socket_timeout_seconds > api.sse_heartbeat_seconds`; keep that validator
in place rather than relying on the defaults happening to be ordered correctly.

### Where retries belong

Put bounded retries in the client only for read paths that have no durable
retry channel — Elasticsearch memory search is the current example. Write paths
covered by an outbox (Qdrant, TEI) get a timeout and nothing else: the outbox
already provides exponential back-off and a dead-letter bound, and stacking a
second retry layer makes the worst-case duration of one cron cycle
unpredictable.

## Scenario: Local Text Embeddings Inference

### 1. Scope / Trigger

Use this contract when changing the local TEI Compose service, its application configuration, or its LangChain Hugging Face client. It prevents the server model, vector shape, and query encoding behavior from drifting independently.

### 2. Signatures

```python
class TEIEmbeddings(HuggingFaceEndpointEmbeddings):
    async def aembed_query(self, text: str) -> list[float]: ...

class TEIEmbeddingClient:
    @classmethod
    def initialize(cls) -> TEIEmbeddings: ...
    @classmethod
    def get_client(cls) -> TEIEmbeddings: ...
    @classmethod
    async def close(cls) -> None: ...
```

### 3. Contracts

- Compose service: `text-embeddings-inference`.
- Shared client modules live in `src/data_agent/infrastructure/`; matching
  integration tests live in `tests/integration/infrastructure/`.
- Image: `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`; no GPU device requests.
- Model: `BAAI/bge-large-zh-v1.5`; output dimension is 1024.
- Endpoint: `conf/app_config.yaml` key `tei.url`, with Hugging Face requests sent to `{url}/embed`.
- Cache: named volume mounted at `/data`.
- Documents receive no query instruction; the LangChain client replaces newlines with spaces. Queries prepend `为这个句子生成表示以用于检索相关文章：`.
- All requests use `normalize=True` and `truncate=True`.
- The managed embedding instance comes from `langchain_huggingface`; only the BGE query instruction is overridden locally.
- Construct it with Pydantic `model_construct`, inject `AsyncInferenceClient(model="{url}/embed")`, and set `client=None` because the standard constructor rejects self-hosted URLs and creates a sync client for accepted repo IDs.
- Pass `normalize=True` and `truncate=True` through `model_kwargs`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| `tei.url` missing or unknown config key present | Pydantic configuration validation fails at startup |
| `get_client()` called before `initialize()` | Raise `RuntimeError` with the initialization instruction |
| Standard `HuggingFaceEndpointEmbeddings(model="http://...")` construction | Rejects the URL; do not use this path |
| Managed client initialization | `client is None` and `async_client` targets `{url}/embed` |
| Input exceeds model token limit | TEI truncates it because `truncate=True` |
| TEI unavailable or returns an HTTP error | Preserve the original `huggingface_hub` exception |
| Model output is not 1024 dimensions | The current integration assertion fails |

### 5. Good / Base / Bad Cases

- Good: initialize once, reuse the manager client, call `aembed_documents` / `aembed_query`, then close it in the executable check's `finally` block.
- Base: a one-item documents batch returns one normalized 1024-dimensional vector.
- Bad: use the standard endpoint constructor for a self-hosted URL, create a sync inference client, omit normalization, or change the Compose model without updating the query instruction and vector dimension.

### 6. Tests Required

```powershell
docker compose -f docs/docker/docker-compose.yml config
docker compose -f docs/docker/docker-compose.yml up -d text-embeddings-inference
uv run python -m data_agent.settings
uv run pytest tests/integration/infrastructure/test_tei_embeddings.py
```

The integration test must assert the LangChain client type, `client is None`, async query/document calls, normalized vectors, and 1024 dimensions. Compose inspection must show a healthy container, the `/data` volume, and no GPU device request.

### 7. Wrong vs Correct

```python
# Wrong: the standard constructor rejects a self-hosted URL.
HuggingFaceEndpointEmbeddings(model="http://localhost:8080/embed")

# Correct: manager injects only the async client.
HuggingFaceEndpointEmbeddings.model_construct(
    client=None,
    async_client=AsyncInferenceClient(model="http://localhost:8080/embed"),
)
```

## Scenario: Rebuildable Memory Search Projections

### 1. Scope / Trigger

Use this contract when changing memory indexing, the ES/Qdrant/TEI projection
payload, outbox dispatch/rebuild, hybrid ranking, or search degradation.

### 2. Signatures

```python
await MemoryElasticsearchIndex.setup() -> None
await MemoryElasticsearchIndex.upsert(projection) -> None
await MemoryElasticsearchIndex.delete(memory_uid) -> None
await MemoryElasticsearchIndex.search(query, source, categories, limit) -> list[str]

await MemoryQdrantIndex.setup() -> None
await MemoryQdrantIndex.upsert(projection, vector) -> None
await MemoryQdrantIndex.delete(memory_uid) -> None
await MemoryQdrantIndex.search(vector, source, categories, limit) -> list[str]

await MemoryIndexDispatcher.dispatch() -> int
await MemoryIndexRebuilder.reset_indexes(
    confirmed_es_index,
    confirmed_qdrant_collection,
) -> None
await MemoryIndexRebuilder.enqueue_batch(after_id=0) -> MemoryRebuildResult
await MemorySearchService.search(
    query,
    source,
    *,
    categories=None,
    limit=None,
    exact_uids=(),
    allowed_object_ids=None,
) -> MemorySearchResponse
```

### 3. Contracts

- MySQL `agent_memory` is authoritative. ES and Qdrant are disposable derived
  projections and may return only candidate UIDs.
- Elasticsearch stores deterministic `memory_text` for BM25. Qdrant stores TEI
  document embeddings under a stable point ID and the same bounded metadata.
- Both indexes filter `source`, optional `category`, `ACTIVE`, `content_version`,
  and `projection_version` before ranking.
- The outbox has one desired state per `(memory_uid, target)`. A target is
  acknowledged independently only after its idempotent upsert/delete succeeds.
  Failures retain the row with bounded exponential retry and a safe exception
  type.
- Full rebuild may recreate only the configured project index/collection after
  the caller supplies exact matching target names, then
  scans ACTIVE MySQL rows by primary-key cursor and enqueues both targets.
- Search runs ES BM25 and TEI/Qdrant concurrently with the configured timeout,
  excludes target signals that still have pending outbox work, and combines
  the remaining ranks with stable RRF.
- Before returning, search batch-loads MySQL and rejects missing, deleted,
  wrong-source, wrong-category, wrong-version, content-hash-mismatched, expired, or
  structurally incompatible rows. Index payload content is never returned.
- One failed target degrades independently. If both fail, the MySQL exact
  baseline remains available.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| ES unavailable or times out | Report ES degradation and continue with Qdrant plus MySQL exact |
| TEI or Qdrant unavailable or times out | Report Qdrant degradation and continue with ES plus MySQL exact |
| Both projection paths fail | Return only validated MySQL exact results |
| A target has pending outbox work for a UID | Exclude that target's ranking signal for that UID |
| Index UID is missing, deleted, stale, wrong-source, or hash-mismatched in MySQL | Reject it |
| External write succeeds but outbox acknowledgement fails | Retry the idempotent desired state |
| TEI vector dimension differs from Qdrant configuration | Raise `ValueError`; retain the outbox row for retry |
| Rebuild is requested | Recreate only configured memory resources and repopulate via cursor batches |

### 5. Good / Base / Bad Cases

- Good: gather candidate UIDs concurrently, remove pending-target signals,
  fuse ranks, and return only current MySQL rows after all contract checks.
- Base: one exact MySQL result remains usable while either derived index is
  unavailable.
- Bad: return ES/Qdrant payloads directly, acknowledge before the external
  write completes, share one acknowledgement across targets, or delete an
  unscoped index/collection during rebuild.

### 6. Tests Required

```powershell
uv run pytest tests/unit/memory/domain/test_memory.py
uv run pytest tests/integration/test_memory_services.py
uv run pytest tests/integration/test_api.py -k memory
uv run pytest -m "not tei"
uv run ruff check src tests
uv run pyright src tests
```

The tests must cover stable RRF, target-independent outbox handling, pending
signal exclusion, MySQL revalidation, content-hash rejection, exact fallback,
soft deletion, API history/update/delete, and rebuild cursor behavior. Live
ES/Qdrant/TEI verification is a separate deployment check when Docker is
available.

### 7. Wrong vs Correct

```python
# Wrong: a derived payload bypasses authority and staleness checks.
return elasticsearch_hit["_source"]

# Correct: indexes contribute only UIDs; MySQL content is reloaded and checked.
candidate_uids = await index.search(query, source, categories, limit)
memories = await repository.get_many_active(candidate_uids)
return validate_and_rank(memories)
```

## Scenario: MySQL Async Engine and Transactional Sessions

### 1. Scope / Trigger

Use this contract when changing the MySQL application configuration, the managed SQLAlchemy async engine, or business-code Session access. It keeps connection health, transaction ownership, concurrency, and shutdown behavior aligned.

### 2. Signatures

```python
class MySQLDatabase:
    @classmethod
    def initialize(cls) -> AsyncEngine: ...
    @classmethod
    def get_client(cls) -> AsyncEngine: ...
    @classmethod
    def session(cls) -> AbstractAsyncContextManager[AsyncSession]: ...
    @classmethod
    async def close(cls) -> None: ...
```

Business code uses one managed context and does not repeat transaction boilerplate:

```python
async with MySQLDatabase.session() as session:
    await session.execute(statement)
```

### 3. Contracts

- Required configuration: `conf/app_config.yaml` key `mysql.url`, using the
  `mysql+asyncmy` driver. `memory.database` separately names the
  schema-qualified application memory database and must differ from the URL's
  default Meta database.
- Construct the engine with `pool_pre_ping=True` and `pool_recycle=3600`.
- Initialize one reusable `async_sessionmaker` bound to the managed engine with `expire_on_commit=False`.
- Create a fresh `AsyncSession` for every `session()` context; never share one Session across concurrent tasks.
- Use that same engine/Session for schema-qualified memory statements; a
  second connection path would break atomic Meta-plus-memory persistence.
- A normal context exit commits. An exceptional exit rolls back and re-raises the original exception. The Session closes on either path.
- Repeated `initialize()` calls reuse the active engine and Session factory.
- `close()` must capture the old engine and clear both shared references before awaiting `old_engine.dispose()`. A concurrent reinitialization during disposal must survive the old close operation.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| `mysql.url` missing, invalid, or an unknown MySQL config key is present | Pydantic configuration validation fails at startup |
| `memory.database` is not a strict identifier or equals the URL default database | Pydantic configuration validation fails at startup |
| `get_client()` called before `initialize()` | Raise `RuntimeError` with the initialization instruction |
| `session()` entered before `initialize()` | Raise `RuntimeError` with the initialization instruction |
| `session()` body and commit complete | Commit once, then close the Session |
| `session()` body or commit raises | Roll back, close the Session, and propagate the exception |
| Two tasks use `session()` concurrently | Give each task a distinct `AsyncSession` bound to the same engine |
| `initialize()` runs while an older engine is disposing | Preserve the replacement engine and Session factory |
| MySQL is unavailable | Preserve the SQLAlchemy/asyncmy connection exception |

### 5. Good / Base / Bad Cases

- Good: initialize during application startup, use one `session()` context per business transaction, and close the manager during application shutdown.
- Base: a managed Session executes `SELECT 1`, commits on exit, and closes.
- Bad: store a global `AsyncSession`, require callers to repeat commit/rollback
  logic, open a second memory-database engine, disable stale-connection
  protection, or clear shared references after awaiting engine disposal.

### 6. Tests Required

```powershell
uv run python -m data_agent.settings
uv run pytest tests/integration/infrastructure/test_mysql.py
uv run ruff check src tests
uv run pyright src tests
```

The focused test must assert engine health settings, factory reuse, `expire_on_commit=False`, distinct concurrent Sessions, automatic commit and rollback, Session closure, live Engine/Session `SELECT 1`, close/reinitialize behavior, and the initialize-during-dispose race.

### 7. Wrong vs Correct

```python
# Wrong: a replacement created while dispose() awaits can be erased here.
await cls._client.dispose()
cls._client = None
cls._session_factory = None

# Correct: detach shared state first and dispose only the captured engine.
client = cls._client
cls._client = None
cls._session_factory = None
if client is not None:
    await client.dispose()
```

## Scenario: Redis Job State and LangGraph Checkpoints

### 1. Scope / Trigger

Use this contract when changing the Redis configuration, public job
projection, arq queue/outbox, source lease, waiting deadline, or LangGraph
checkpoint lifecycle.

### 2. Signatures

```python
RedisClient.initialize() -> Redis
await CheckpointStore.initialize() -> AsyncRedisSaver
DDLJobStore(redis).submit(request) -> JobRecord
RedisJobStateStore(redis, keys).transition(...) -> bool
SourceLeaseStore(redis, keys).renew(source, job_id) -> bool
JobOutboxStore(redis, keys).dispatch(queue, limit=100) -> int
JobEventStore(redis, keys).publish(job_id, event_type, data) -> str
JobEventStore(redis, keys).read_after(job_id, after_id, ...) -> list[JobEvent]
```

### 3. Contracts

- Local Compose and CI use `redis:8.8.0`; Redis 8 supplies RedisJSON and
  RediSearch required by `langgraph-checkpoint-redis`.
- Compose publishes `127.0.0.1:6379`, mounts `/data`, and enables AOF with
  `appendfsync everysec`. Normal process/container restart is recoverable; an
  abrupt host loss may lose about one second of acknowledged Redis writes.
- `RedisClient` owns the decoded application client used by `DDLJobStore`.
  `CheckpointStore` owns a separate `AsyncRedisSaver`, explicitly
  enters its async context, awaits `asetup()`, and closes that same context.
- Consumers import the application-facing facade from
  `data_agent.ddl_metadata.jobs.store`. The facade composes
  `RedisJobStateStore`, `SourceLeaseStore`, `JobOutboxStore`, and
  `JobEventStore`; API, worker, and memory services do not construct those
  specialized stores separately.
- Redis-specific job modules live under `ddl_metadata/jobs/redis/`.
  Stateful persistence collaborators retain the `Store` suffix, while pure
  `JobKeys`, `JobCodec`, and `JobScripts` centralize key formatting, canonical
  payload conversion, and Lua text so those protocols have one implementation
  source.
- The public source of truth is `ddl:job:{job_id}` plus revision-aware
  transitions. LangGraph checkpoints are recovery state and arq result keys are
  not public API records.
- Submission atomically writes the job Hash, source lease, and dispatch outbox
  before returning `202`. Answer submission atomically validates revision,
  question-set ID, deadline, and payload hash before scheduling the next
  revision.
- Queue IDs contain job ID plus revision. Worker graph invocations reuse
  `thread_id=job_id` and `durability="sync"`.
- Worker graph execution fully consumes LangGraph v2 `tasks` streaming. Only
  task-start node names are mapped to stable public business stages; task
  input, result, interrupt, error, raw DDL, prompt, and checkpoint payloads
  never enter the public event contract.
- Each job's public notifications use
  `ddl:job:{job_id}:events`. `XADD MAXLEN ~` applies the configured approximate
  length bound and every append refreshes the same TTL used by retained job
  results. The Stream remains a notification log; the job Hash is authoritative.
- Event publication happens after the authoritative state transition. A
  publication failure is logged with safe typed fields and does not roll back
  or fail successful business work; SSE timeout repair rereads the Hash.
- A reclaimed arq activation may legitimately find the public projection still
  at `running`. When its revision matches, the worker reconciles and resumes
  the existing checkpoint instead of requiring another `pending -> running`
  transition.
- One renewable logical-source lease spans waiting-input rounds. Browser memory
  mutations acquire the same lease briefly and fail with `source_busy` when a
  graph owns it.
- Terminal/expired paths release the source lease, apply result retention, and
  atomically add the job to the checkpoint-cleanup outbox. The worker removes
  that outbox item only after `adelete_thread()` succeeds, so Redis timeouts or
  worker restarts retry cleanup instead of leaking terminal checkpoints.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Redis unavailable during submit | Return `503`; do not report an accepted job |
| Duplicate queue activation | Revision/status guard makes it a no-op |
| Reclaimed activation with matching `running` revision | Resume/reconcile the existing checkpoint |
| Duplicate identical answer | Return the existing next revision without enqueueing twice |
| Answer references an unknown or duplicate question ID | Return `422`; do not enqueue or checkpoint it |
| Stale/conflicting answer | Return `409` and preserve the current round |
| Answer after deadline | Atomically reject with `410`; do not resume the graph |
| Checkpoint interrupt but public state still running | Worker repairs projection to `waiting_input` |
| Persist failure after prior checkpoints | Resume `persist_snapshot` without repeating completed model nodes |
| Missing Redis modules or failed saver setup | Fail worker startup and close the partial saver |
| Public event append fails after a state transition | Preserve the state transition; repair clients from the authoritative Hash |
| Event Stream exceeds its configured target length | Redis approximately trims old notifications and retains a bounded tail |

### 5. Good / Base / Bad Cases

- Good: a terminal transition writes the cleanup outbox atomically; a failed
  `adelete_thread()` leaves the item for the next worker sweep.
- Base: duplicate activation or duplicate answer is absorbed by the
  revision/status guard without another graph execution.
- Bad: deleting a checkpoint before the terminal transition, or acknowledging
  cleanup after a Redis timeout, loses the only durable cleanup request.

### 6. Tests Required

```powershell
docker compose -f docs/docker/docker-compose.yml config
uv run pytest tests/integration/infrastructure/test_redis.py
uv run pytest tests/integration/test_job_events.py
uv run pytest tests/integration/test_worker.py
uv run pytest tests/integration/test_ddl_metadata_flow.py
```

The combined integration module requires both Redis and MySQL. It must prove
interrupt/resume, public-state transitions, accepted snapshot persistence, and
compatible-memory reuse.

### 7. Wrong vs Correct

```python
# Wrong: cleanup loss when thread deletion fails.
await checkpointer.adelete_thread(job_id)
await redis.zrem(cleanup_key, job_id)

# Correct: DDLJobStore atomically schedules cleanup; acknowledge only on success.
job = await job_store.transition_terminal(...)
await checkpointer.adelete_thread(job.job_id)
await job_store.ack_checkpoint_cleanup(job.job_id)
```

## Scenario: OpenAI-Compatible Structured Metadata Model

### 1. Scope / Trigger

Use this contract when changing LLM configuration, structured-output models,
semantic/metric prompt boundaries, or worker startup capability checks.

### 2. Signatures

```python
LLMClient.initialize() -> ChatOpenAI
await LLMClient.check_structured_output_capability() -> None
LLMMetadataGenerator.classify(...) -> SemanticMetadata
```

### 3. Contracts

- YAML contains only `llm.base_url`, model name, timeouts, confidence,
  concurrency/retry, structured-output method, and version identifiers.
- The API key comes only from `DATA_AGENT_LLM_API_KEY`; absence fails
  initialization. It is never added to YAML, Redis, checkpoints, logs, or API
  responses.
- `ChatOpenAI` uses temperature zero and the configured
  `json_schema` or `function_calling` method.
- Worker startup performs a live Pydantic structured-output capability probe.
  There is no plain-text, `json_mode`, or best-effort fallback.
- The parser supplies all physical names, types, comments, keys, and stable
  IDs. The model receives bounded physical-schema JSON and can provide only
  semantic roles, descriptions, aliases, questions, and metrics.
- Every model response crosses Pydantic validation and deterministic
  AST/reference/confidence validation before persistence.
- Model calls may repeat only if the process fails before the completed node is
  checkpointed. Completed model nodes are reused on later persistence retry.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Missing environment API key | Fail startup with actionable `RuntimeError` |
| Unsupported configured structured-output method | Fail capability probe; do not degrade to text |
| Timeout, connection error, 429, or 5xx | Bounded worker retry from checkpoint |
| Authentication/configuration failure | Terminal failure without secret details |
| Hallucinated object, role conflict, or low confidence | Repair once when allowed, then reject without writes |
| Missing metric business meaning | Interrupt for explicit user input; never guess |

### 5. Good / Base / Bad Cases

- Good: a structured response passes Pydantic and deterministic AST/reference
  validation before graph state or memory is accepted.
- Base: one malformed semantic response receives the bounded repair attempt;
  persistent invalid output becomes a business rejection with zero writes.
- Bad: parsing model prose or accepting a dictionary without current schema,
  identity, role, and confidence validation.

### 6. Tests Required

```powershell
uv run pytest tests/unit/infrastructure/test_llm_client.py
uv run pytest tests/unit/ddl_metadata/test_validation.py
uv run pytest tests/unit/ddl_metadata/workflow/test_graph.py
```

These CI checks use deterministic fakes/mocks and do not contact or require a
paid/live LLM. A real endpoint capability probe is an explicit deployment
check and must be reported separately.

### 7. Wrong vs Correct

```python
# Wrong: best-effort text fallback bypasses the typed contract.
result = json.loads(await model.ainvoke(prompt))

# Correct: capability is checked at startup and every response is typed.
structured = model.with_structured_output(ResponseModel, method=method)
result = await structured.ainvoke(messages)
```

## Scenario: Conversation Memory Extraction

Completed text turns are claimed from MySQL with a short lease and committed
before the structured LLM call starts. Only the oldest pending turn per
conversation is eligible, and each wave claims at most the configured LLM
concurrency. The model returns a bounded summary and zero or more typed
user-memory candidates. Runtime validation requires every
candidate to reference a message owned by the same user and conversation, an
exact non-empty user quote, and, for an assistant-originated conclusion, an
exact assistant quote plus a later user quote that repeats the confirmed
conclusion. Ambiguous assent, unrelated statements, duplicate result scopes,
and assistant-only claims are discarded.

Finalization uses a fresh MySQL transaction and the lease token as a
compare-and-set boundary. It advances the summary cursor monotonically,
upserts accepted candidates through the existing authoritative memory/event/
index-outbox path, and removes the extraction row. Failure clears the lease
and applies bounded exponential backoff without changing persisted messages.
