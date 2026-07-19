# LangGraph DDL Metadata Conversion Implementation Plan

DDL-specific services live under `app/service/ddl_metadata/`, persistence lives
under `app/repository/ddl_metadata/`, and the worker entry point is
`app/worker/ddl_metadata.py`. Tests mirror the service and repository domain
packages.

## Execution order

### 1. Add runtime dependencies and strict configuration

- Add compatible bounded dependencies for FastAPI/Uvicorn, SQLGlot,
  `langgraph>=1.2,<1.3`, `langgraph-checkpoint-redis>=0.5,<0.6`,
  `langchain-openai>=1.3,<1.4`, and `arq>=0.28,<0.29`.
- Pin the shared Redis Python client to `redis>=5.2.1,<6`, the compatible
  intersection for `arq 0.28` and the selected checkpoint package.
- Regenerate `uv.lock` and verify Python 3.13 compatibility.
- Extend `AppConfigModel` and `conf/app_config.yaml` with typed API, Redis, LLM,
  memory-version/rebuild, input-limit, retention, timeout, and
  worker-concurrency settings.
- Add `memory.database` with a strict MySQL identifier contract, default it to
  `data_agent`, and reject using the default database from `mysql.url`.
- Read the model API key only from `DATA_AGENT_LLM_API_KEY`; keep it out of the
  YAML model and configuration self-check output.
- Extend the local Compose file with Redis 8 (including the JSON/Search
  capabilities required by the checkpoint package), loopback port publication,
  AOF, health check, and named volume.
- Add an idempotent `data_agent.sql` application bootstrap owning
  `llm_memory` and `llm_memory_relation`; keep `meta.sql` limited to the four
  Meta tables.

Validation gate:

```powershell
uv sync --locked
uv lock --check
uv run python -m app.conf.app_config
docker compose -f docs/docker/docker-compose.yml config
```

Rollback point: dependency/config/Compose-only diff; no runtime code or data
changes yet.

### 2. Establish typed contracts and managed clients

- Add shared Pydantic contracts for physical schema, semantic metadata, metric
  questions/answers, final metrics, canonical/derived memory, validation
  issues, job states, and API envelopes.
- Add managed async Redis and OpenAI-compatible chat-model lifecycles using the
  repository's existing `initialize/get_client/close` pattern.
- Keep checkpoint creation explicit and lifecycle-owned; do not initialize
  clients from package `__init__.py`.
- Add focused manager checks for configuration, reuse, pre-initialize errors,
  and async cleanup.
- Add an explicit structured-output capability check for the configured
  `json_schema` or `function_calling` method; never fall back to plain text.

Validation gate:

```powershell
uv run python -m app_test.client.test_redis_client_manager
uv run python -m app_test.client.test_llm_client_manager
```

### 3. Implement deterministic DDL parsing and validation

- Parse only MySQL `CREATE TABLE` ASTs with SQLGlot.
- Enforce encoded-size/table/column limits and reject every unsupported
  statement kind before model invocation.
- Produce one canonical `PhysicalSchema`, exact physical-role assignments, and
  the canonical DDL hash.
- Implement deterministic stable table/column/metric IDs.
- Add validation that compares model output sets and roles against the parsed
  schema and returns typed issues rather than booleans/strings.
- Exercise multi-table, inline/table constraints, comments, factless facts,
  duplicate objects, malformed SQL, unsupported SQL, and limit cases.

Validation gate:

```powershell
uv run python -m app_test.service.ddl_metadata.test_parser
uv run python -m app_test.service.ddl_metadata.test_validator
```

Rollback point: pure contracts/parser/validator code; no external writes.

### 4. Implement transactional Meta snapshot synchronization

- Add focused repositories owning static bound statements for the four
  unqualified Meta tables plus schema-qualified `llm_memory` and
  `llm_memory_relation` in `memory.database`.
- Use the existing `MysqlClientManager.session()` transaction.
- Implement scoped upsert, stale link/column cleanup, and orphan metric cleanup
  for submitted table IDs only.
- Preserve rows outside the submitted scope.
- Implement exact compatible-memory retrieval, batch relation loading, pinned
  user-confirmed precedence, append/supersede/archive lifecycle, and stable
  memory UIDs.
- Implement bounded list/detail projections plus idempotent pin/archive and
  typed correction service methods. Reuse the logical-source lease so a
  browser mutation cannot race with an active graph.
- Keep question/answer memories immutable. Correction creates a
  user-confirmed replacement and `SUPERSEDES` relation atomically but leaves
  Meta unchanged until a later validated DDL run.
- Implement a bounded memory-payload rebuild service that derives payload from
  canonical content, continues after per-row failures, and reports counts.
- Commit accepted Meta rows, long-term memories, relations, and supersession in
  the same managed cross-database transaction; do not add a second engine or
  Session.
- Add live MySQL checks for initial insert, exact repeat, changed snapshot,
  unrelated-table preservation, memory reuse/supersession, archived exclusion,
  conflicting active memory, schema separation, a real memory-side failure
  rolling back prior Meta writes, and safe re-execution after a simulated
  post-commit crash.

Validation gate:

```powershell
Get-Content docs/docker/mysql/data_agent.sql |
    docker compose -f docs/docker/docker-compose.yml exec -T mysql `
        mysql -uroot -proot
uv run python -m app_test.repository.ddl_metadata.test_meta
uv run python -m app_test.repository.ddl_metadata.test_memory
uv run python -m app_test.service.ddl_metadata.test_memory
```

Safety: run destructive snapshot checks only against disposable local test
IDs/databases. Do not reset or delete the developer's shared MySQL volume.

### 5. Build and compile the LangGraph workflow

- Implement the serializable graph state and nodes described in `design.md`.
- Load and validate exact compatible long-term memory after AST parsing; pass
  only a bounded typed capsule to semantic/metric nodes.
- Use parser-derived compact JSON and Pydantic structured output for table and
  column semantics.
- Use native LangGraph fan-out only for bounded independent schema groups; cap
  concurrency and merge through one typed reducer.
- Add deterministic metadata and metric validators with one technical
  correction attempt.
- Implement metric question planning, `interrupt`, `Command(resume=...)`, the
  two-round limit, and no-metric dimension-only path.
- Ensure interrupt nodes have no non-idempotent side effect before
  `interrupt()`, because resume restarts the node from its beginning.
- Initialize the async Redis checkpointer with its setup call, compile with
  `thread_id = job_id`, and invoke with synchronous checkpoint durability.
- Bind `trace_id=job_id` and emit safe node/attempt/timing logs.
- Use a deterministic fake model to test success, hallucination rejection,
  correction exhaustion, follow-up questions, metric reference validation,
  unchanged-memory reuse, stale-memory invalidation, and conflict handling.

Validation gate:

```powershell
uv run python -m app_test.service.ddl_metadata.test_graph
```

Rollback point: graph can be tested without API or worker processes.

### 6. Add Redis job projection, queue worker, and recovery

- Implement the centralized revision-aware job transition function and Redis
  key prefixes.
- Implement the application job hash, dispatch sorted-set outbox, waiting
  deadline sorted set, and startup/periodic dispatcher. Return `202` only after
  the job plus outbox transaction succeeds.
- Enforce one renewable active-job lease per logical source through every
  `waiting_input` round and release it on all terminal transitions.
- Queue revision-specific `arq` executions under a stable public job ID.
- Add the worker entry point, bounded retries with jitter, error
  classification, and resume-from-checkpoint behavior.
- Add the 30-minute/two-round expiry scheduler and 24-hour terminal retention.
- Make answer submission compare-and-set on status, revision, question-set ID,
  payload hash, and expiry in one Lua or `WATCH`/`MULTI` transition that also
  schedules the next dispatch generation.
- Reconcile public job projection from the latest checkpoint at worker entry;
  never blindly replay initial input.
- Verify duplicate answers do not enqueue duplicate revisions.
- Verify graceful worker cancellation requeues immediately and a hard crash is
  reclaimed after `arq`'s in-progress TTL before resuming the same checkpoint.
- Force MySQL persistence to fail once and assert the retry does not repeat any
  completed LLM call.

Validation gate:

```powershell
uv run python -m app_test.worker.test_ddl_metadata
```

### 7. Add the loopback-only FastAPI boundary

- Replace the placeholder `main.py` behavior with an application factory and
  lifespan that configures logging and explicitly initializes/closes clients.
- Add submit, status, and answer routes with the exact contracts in
  `design.md`.
- Add the bounded memory list/detail, pin/archive patch, and correction routes
  from `design.md`; do not add hard delete, arbitrary content editing, or
  Memos social/note features.
- Enforce byte/count limits, configured CORS allowlist, safe error mapping, and
  `127.0.0.1` default host.
- Map Redis submission outages to `503`, stale answers to `409`, expired
  answers to `410`, unknown jobs to `404`, and request validation to `422`.
- Do not add authentication or a CLI.

Validation gate:

```powershell
uv run python -m app_test.api.test_ddl_metadata_api
```

### 8. Run end-to-end and recovery checks

- Start local Redis and MySQL through the existing Compose project.
- Apply `docs/docker/mysql/data_agent.sql` through the local MySQL root account
  when reusing an initialized volume; bootstrap scripts run automatically only
  for a fresh volume. Do not drop or migrate legacy Meta memory tables.
- Submit a multi-table fact/dimension DDL.
- Poll until `waiting_input`, submit metric answers, and poll until
  `succeeded`.
- Verify all four Meta tables and rerun the identical request.
- Verify accepted memory records/relations, exact reuse, and that rejected jobs
  create no trusted memories.
- Verify list/detail projection, pin/unpin, archive, correction supersession,
  source-busy rejection, and that correction leaves Meta unchanged until the
  source DDL is reprocessed.
- Submit a changed DDL and verify scoped stale cleanup.
- Simulate each documented exception boundary: parser rejection, invalid model
  output, metric timeout, worker interruption, Redis outage, and MySQL
  rollback/retry.
- Simulate a crash after accepted job/outbox creation but before arq enqueue,
  and verify the dispatcher eventually queues exactly one generation.
- Document the approximately one-second abrupt-host-loss window of local Redis
  `appendfsync everysec`; do not claim literal zero-loss-after-`202`.
- Capture exact unavailable-service limitations instead of claiming a skipped
  live check passed.

Suggested commands:

```powershell
docker compose -f docs/docker/docker-compose.yml up -d mysql redis
uv run python -m app_test.integration.test_ddl_metadata_flow
```

### 9. Update repository specs and CI

- Update backend directory, database, error-handling, logging, quality, and
  external-service integration specs to describe only the patterns that now
  exist.
- Add the Redis CI service and focused deterministic checks. Do not require a
  live paid LLM in CI.
- Keep existing MySQL and TEI contracts intact.
- Read `code_review.md` before the Trellis check pass.

Full quality gate:

```powershell
uv sync --locked
uv lock --check
uv run --with ruff ruff check app app_test
uv run --with pyright pyright app app_test
uv run python -m compileall -q app app_test main.py
uv run python -m app.conf.app_config
uv run python -m app_test.core.test_logging
uv run python -m app_test.client.test_mysql_client_manager
uv run python -m app_test.client.test_redis_client_manager
uv run python -m app_test.service.ddl_metadata.test_parser
uv run python -m app_test.service.ddl_metadata.test_validator
uv run python -m app_test.repository.ddl_metadata.test_meta
uv run python -m app_test.repository.ddl_metadata.test_memory
uv run python -m app_test.service.ddl_metadata.test_memory
uv run python -m app_test.service.ddl_metadata.test_graph
uv run python -m app_test.worker.test_ddl_metadata
uv run python -m app_test.api.test_ddl_metadata_api
uv run python -m app_test.integration.test_ddl_metadata_flow
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

## Review gates

1. Dependency and Compose choices match the research evidence and Python 3.13.
2. Physical DDL facts have exactly one owner: the deterministic parser.
3. Every LLM response crosses one Pydantic plus deterministic validation
   boundary.
4. No path can write Meta rows before metric clarification succeeds.
5. Redis/public job status and LangGraph checkpoints do not become competing
   public state sources.
6. Every retry boundary is tested for duplicate LLM calls and duplicate MySQL
   effects.
7. Snapshot cleanup cannot touch tables absent from the submitted DDL.
8. Secrets/raw DDL/user answers do not appear in logs or API errors.
9. Browser memory correction preserves history and cannot partially patch Meta
   or race an active source job.
10. The final implementation stays within local unauthenticated deployment
   scope.

## Rollback considerations

- Stop API and worker before rolling back dependencies or Redis configuration.
- Preserve the Redis volume while inspecting in-flight jobs; deleting it is
  destructive and intentionally outside normal rollback.
- A code rollback does not reverse successfully accepted Meta snapshots.
- If stable-ID inputs or normalization change during implementation, stop and
  revise the design before writing data; silently changing IDs would duplicate
  existing metadata.
