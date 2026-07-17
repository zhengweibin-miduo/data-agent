# Research: LangGraph, Redis, and async recovery

- Query: Design a Python 3.13-compatible LangGraph workflow using OpenAI-compatible `ChatOpenAI` structured output, Redis checkpoints, and a lightweight async Redis queue, with exact recovery behavior.
- Scope: mixed
- Date: 2026-07-17

## Findings

### Recommendation

Use one Redis 8 server for three deliberately separate responsibilities:

1. arq is only the at-least-once activation queue.
2. An application-owned Redis job record is the public status/result source of truth.
3. `AsyncRedisSaver` is only the LangGraph checkpoint store.

Use arq rather than Celery. This repository needs one asyncio worker function, Redis is already required, and arq supplies bounded concurrency, atomic multi-worker claiming, retry/defer support, custom job-ID uniqueness, health keys, and pessimistic re-execution. Celery 5.6.3 is Python 3.13-compatible, but adds Kombu, billiard, vine, and a broader task-processing model without closing any recovery gap that this task actually has. arq is not exactly-once; the graph and MySQL node still must be idempotent.

The currently published compatible package set is:

| Package | Verified current version | Python constraint / important dependency |
|---|---:|---|
| `langgraph` | 1.2.9 | Python `>=3.10`; `langchain-core>=1.4.7,<2` |
| `langgraph-checkpoint-redis` | 0.5.1 | Python `>=3.10,<3.15`; `redis>=5.2.1`, `redisvl>=0.15,<1` |
| `langchain-openai` | 1.3.5 | Python `>=3.10,<4`; `langchain-core>=1.4.9,<2`, `openai>=2.45,<3` |
| `arq` | 0.28.0 | Python `>=3.9`; explicitly classifies Python 3.13; `redis[hiredis]>=4.2,<6` |
| `redis` client | 5.x | The shared satisfiable range is **`redis>=5.2.1,<6`**, not the current redis-py 8.x |
| `fastapi` | 0.139.2 | Python `>=3.10`; compatible with Pydantic 2 |
| `uvicorn` | 0.51.0 | Python `>=3.10` |

Recommended dependency ranges are therefore `langgraph>=1.2,<1.3`, `langgraph-checkpoint-redis>=0.5,<0.6`, `langchain-openai>=1.3,<1.4`, `arq>=0.28,<0.29`, and an explicit shared `redis>=5.2.1,<6`. Let `uv.lock` pin exact artifacts. Do not independently upgrade redis-py to 8 while arq 0.28 is installed.

### Redis and checkpointer requirements

`langgraph-checkpoint-redis` is not compatible with an arbitrary old, module-free Redis image. It requires RedisJSON and RediSearch. Redis 8 includes both in the server; Redis below 8 needs Redis Stack or separately installed modules. Pin a Redis 8 image (the current Redis release is 8.8.0) and mount `/data` to a named volume.

Create one long-lived async saver during worker startup, call `await checkpointer.asetup()` before compiling/using the graph, and close it at shutdown. `asetup()` creates the required search indexes; omitting it is an invalid design. Use the full `AsyncRedisSaver`, not the in-memory saver. A shallow saver could reduce history, but the graph is short and full checkpoints retain the clearest pending-write/fault history; terminal cleanup prevents unbounded growth.

Compile the graph once with that saver. Use the opaque public job UUID as the stable LangGraph `thread_id` for every initial invocation, retry, and answer resume:

```python
config = {"configurable": {"thread_id": job_id}}
await graph.ainvoke(input_or_command, config, durability="sync")
```

`durability="sync"` is important: LangGraph then persists a completed step before starting the next step. `"async"` has a small crash window; `"exit"` cannot recover intermediate work after a process crash. A checkpoint is at a super-step boundary, not an arbitrary Python line.

### Graph boundaries that make retry safe

Keep each expensive or externally visible operation in its own async node:

```text
parse DDL
  -> semantic LLM call
  -> deterministic validation/correction routing
  -> metric-question LLM call
  -> interrupt-only node
  -> metric-answer LLM call
  -> deterministic final validation
  -> MySQL persistence
```

This boundary is what satisfies “retry MySQL without repeating completed LLM calls.” Once final validation has completed under sync durability, the checkpoint contains the validated table/column/metric payload and `next=("persist",)`. A transient persist failure resumes that checkpoint and executes only `persist`.

Do not generate questions in the same node before `interrupt()`. LangGraph restarts the entire interrupted node from its beginning on resume, so code before `interrupt()` runs again. The interrupt node should only build a small JSON-serializable payload from already-checkpointed state and call `interrupt(payload)`. Resume with the same `thread_id` and `Command(resume=answer)`.

Use explicit retry predicates rather than the broad default:

- LLM node: retry only transport timeout/connection errors and retryable 429/5xx responses; small bounded attempts with backoff.
- Validation errors and business ambiguity route through graph state and eventually `rejected`; they are not infrastructure retries.
- Persist node: retry only transient SQLAlchemy/asyncmy failures such as connection loss, lock timeout, and deadlock; do not retry `IntegrityError` or deterministic contract failures blindly.
- Put a LangGraph async node timeout around each network node. LangGraph 1.2 timeouts are async-only; timed-out writes are cleared before its retry policy runs.

An unavoidable boundary remains: if a worker dies after an OpenAI-compatible server produced a response but before the node result reached Redis, the LLM call can repeat. Neither LangGraph nor arq can provide exactly-once execution across that external boundary, and generic OpenAI-compatible endpoints do not share a guaranteed idempotency protocol. The design can guarantee “no repeat after the LLM node checkpoint exists,” not “an LLM HTTP request is never repeated.”

`ChatOpenAI` supports `model`, `base_url`, `timeout`, `max_retries`, and an API key from `OPENAI_API_KEY`. `with_structured_output(PydanticModel)` returns a validated Pydantic instance; non-Pydantic schemas do not receive the same client-side validation. In `langchain-openai` 1.3.5 the `ChatOpenAI` override defaults to `method="json_schema"`, but arbitrary OpenAI-compatible servers may not implement OpenAI native Structured Outputs. Treat capability as a startup/live check. If the selected endpoint only implements tool calling, explicitly use `method="function_calling"`; do not silently fall back to unconstrained text/`json_mode`.

### Job record and dispatch contract

Do not expose arq's result key as the API job record. arq result retention is worker-oriented and its custom job-ID uniqueness ends when the queued job/result keys clear. Keep a compact application-owned Redis record:

```text
ddl:job:{job_id}
  status, generation, attempt, created_at, updated_at, deadline_at,
  question_json, result_json, error_json, graph_version
ddl:dispatch                         # sorted-set outbox of job_id:generation
ddl:waiting                          # sorted-set of waiting deadline -> job_id:generation
```

The API submission transaction writes `status=pending` and adds generation 0 to `ddl:dispatch` before returning `202`. A dispatcher then calls:

```python
enqueue_job("run_ddl_job", job_id, generation,
            _job_id=f"ddl:{job_id}:{generation}")
```

and removes the outbox item only after enqueue succeeds or returns `None` because that deterministic arq job already exists. A periodic/startup dispatcher retries remaining outbox entries. This closes the API-crash window between recording an accepted job and enqueueing it without depending on arq internals.

Answer submission uses one Lua script (or an equivalent WATCH/MULTI loop) to:

1. require `status=waiting_input`, the expected generation, and `deadline_at > now`;
2. store the answer, increment generation, set `status=pending`;
3. remove the waiting deadline and add the new activation to `ddl:dispatch`.

This makes duplicate answers deterministic (`409`/already answered), and a concurrent timeout sweep cannot both reject and resume the same generation. The worker likewise changes `pending -> running` and terminal states with generation-checked atomic transitions. arq's custom job ID prevents concurrent execution of one activation; these state guards make late duplicate activations harmless.

Use `keep_result=0` for the arq function because the application job record owns results. Set `max_jobs` to the desired LLM concurrency. Set a finite arq function timeout slightly above the maximum graph activation time, while keeping shorter per-node timeouts inside LangGraph. arq's outer timeout is a safety kill, not the primary retry policy.

### Recovery procedure

At worker entry, do not always replay the initial input. Inspect the latest checkpoint and job generation:

1. Terminal or `waiting_input` job record: return without graph execution.
2. No checkpoint: invoke the graph with the stored initial DDL state.
3. Latest checkpoint contains an interrupt and this generation has a stored answer: invoke `Command(resume=answer)`.
4. Latest checkpoint contains an interrupt but no answer: repair the public projection to `waiting_input`; do not invoke.
5. `StateSnapshot.next == ()`: project the checkpointed final result/status into the public job record; do not re-run the graph.
6. Otherwise invoke with `None` to continue from the latest checkpoint.

This projection repair handles crashes between a durable graph write and the public status update.

| Failure | Exact behavior |
|---|---|
| API process restarts | No work is owned in memory. Status, outbox, queue, and checkpoints remain in Redis; startup dispatch drains outbox. |
| Graceful worker shutdown during a job | arq cancels the coroutine; with `retry_jobs=True`, `CancelledError` leaves the job in its sorted-set queue and it runs again. The graph resumes from the latest durable checkpoint. |
| Hard worker crash | arq leaves the queue entry. Its atomic `arq:in-progress:<id>` key blocks other workers until it expires; arq 0.28 sets that TTL to the configured maximum job timeout plus 10 seconds. Recovery can therefore be delayed by that amount. |
| Multiple workers | arq WATCHes the in-progress key and atomically `PSETEX` claims it before starting; only one worker starts a given arq job ID. Application generation checks are still required for stale/duplicate activations. |
| Crash after an LLM node checkpoint | Retry skips that completed node and continues at the next node. |
| Crash inside an LLM request before checkpoint | The request may repeat. This is at-least-once and must be documented. |
| MySQL error before commit | Existing managed Session rolls back and re-raises. Persist-node retry reloads the validated checkpoint and makes no LLM calls. |
| Crash after MySQL commit but before graph checkpoint acknowledgment | Persist node can run again. Stable IDs, upserts, scoped deletes, and one transaction must make the complete snapshot operation idempotent. |
| Redis unavailable | API must not return `202` unless the job+outbox transaction succeeded. Workers fail/reconnect; MySQL must never be used as a substitute queue. |
| Redis restarts normally with AOF and volume | Redis replays the AOF; queue, outbox, public status, and checkpoints recover. Workers then resume/reconcile as above. |
| Host/power crash with `appendfsync everysec` | Redis documents that up to about one second of acknowledged writes may be lost. This does **not** meet literal zero-loss-after-202 semantics. |

For local development, `appendonly yes`, `appendfsync everysec`, and a named `/data` volume are a practical default, with the one-second disaster window documented. If every returned `202` must survive abrupt host/power loss, use `appendfsync always` (slower) or an externally managed durable Redis deployment; a Docker volume alone is not a durability guarantee. No Redis configuration provides recovery from deliberate volume deletion or `FLUSHALL`.

### Waiting timeout and retention

LangGraph interrupts wait indefinitely; Redis TTL alone does not transition the public state. Maintain `ddl:waiting` and run a small arq cron/sweeper at least once per minute. For every due entry, a Lua transition checks the same generation and `status=waiting_input`, then:

1. marks the job `rejected` with a stable timeout code;
2. removes the waiting/outbox entries;
3. calls `await checkpointer.adelete_thread(job_id)` (cleanup is idempotent);
4. applies the terminal job-record TTL.

The answer API performs the same deadline check, so an answer cannot win after the deadline merely because the sweep has not run yet. Repeat the same cleanup on `succeeded`, `rejected`, and non-retryable/exhausted `failed`.

Recommended initial retention contract:

- `pending` / `running`: no TTL; recovery owns them.
- `waiting_input`: explicit 30-minute deadline, not passive expiration.
- terminal job record/result/error: 24-hour TTL.
- LangGraph checkpoints: no default TTL while active; explicit `adelete_thread` on terminal transition.
- arq result: zero seconds (`keep_result=0`).
- cleanup reconciliation: startup plus periodic sweep, so a crash during terminal cleanup does not leak checkpoints permanently.

The 24-hour terminal retention is a proposed default because the requirements do not specify a value. Make it one named application setting only if product needs differ.

### Repository fit

- Python is already constrained to `>=3.13,<3.14` (`pyproject.toml:6-7`), and none of the required runtime packages conflicts with 3.13.
- The current dependency list has no FastAPI, LangGraph, `langchain-openai`, Redis, arq, or DDL parser (`pyproject.toml:8-19`).
- Configuration is strict Pydantic with `extra="forbid"` and is loaded once from YAML (`app/conf/app_config.py:9-12`, `app/conf/app_config.py:78-105`). Redis URL, model/base URL, timeouts, allowed origins, and bind address belong there; the model API key remains environment-only.
- Current Compose has MySQL, Qdrant, Elasticsearch, and TEI plus named volumes, but no Redis (`docs/docker/docker-compose.yml:1-67`).
- `MysqlClientManager.session()` creates a fresh async Session, commits on normal exit, rolls back and re-raises on failure (`app/client/mysql_client_manager.py:49-65`). The whole four-table snapshot belongs inside one such context.
- The Meta schema has table, column, metric, and join tables with primary keys suitable for deterministic upsert/link idempotency (`docs/docker/mysql/meta.sql:7-48`).
- Application-owned long-term memory belongs in a separate configurable MySQL
  database. Schema-qualified memory statements can share the existing engine,
  connection, and Session with Meta so one InnoDB transaction covers both
  databases without adding another transaction coordinator.
- The task explicitly requires Redis-owned queue/status/checkpoints, bounded duplicate-safe workers, sync recovery, and idempotent replay after commit (`.trellis/tasks/07-17-langgraph-ddl-metadata-conversion/prd.md:79-93`).

## Files Found

- `pyproject.toml` — Python 3.13 constraint and current dependency baseline.
- `uv.lock` — current resolved environment; none of the proposed queue/graph packages is present.
- `app/conf/app_config.py` — strict shared configuration model and load boundary.
- `conf/app_config.yaml` — current service URLs; no Redis/LLM/API section.
- `app/client/mysql_client_manager.py` — reusable async transaction boundary.
- `docs/docker/docker-compose.yml` — local infrastructure and named-volume pattern; no Redis service.
- `docs/docker/mysql/meta.sql` — four Meta business tables and keys.
- `docs/docker/mysql/data_agent.sql` — idempotent application-database
  bootstrap for canonical memory and typed relations.
- `.trellis/tasks/07-17-langgraph-ddl-metadata-conversion/prd.md` — authoritative task recovery and status requirements.

## External References

- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts.md — same-thread resume, `Command(resume=...)`, whole-node restart, and idempotent side-effect rules.
- LangGraph checkpointers: https://docs.langchain.com/oss/python/langgraph/checkpointers.md — super-step checkpoints, pending writes, fault recovery, replay, and durability modes.
- LangGraph fault tolerance: https://docs.langchain.com/oss/python/langgraph/fault-tolerance.md — `RetryPolicy`, async node timeouts, retry ordering, and graceful drain behavior for LangGraph 1.2.
- LangChain model structured output: https://docs.langchain.com/oss/python/langchain/models.md — Pydantic validation and structured-output methods.
- ChatOpenAI integration/reference: https://docs.langchain.com/oss/python/integrations/chat/openai.md and https://reference.langchain.com/python/langchain-openai/chat_models/base/ChatOpenAI.md — `base_url`, environment key, retries/timeouts, and provider caveats.
- LangGraph Redis 0.5.1 source/README: https://github.com/redis-developer/langgraph-redis/tree/0.5.1 — Redis module requirement, `AsyncRedisSaver.asetup`, TTL, and `adelete_thread`.
- arq 0.28 documentation/source: https://arq-docs.helpmanual.io/ and https://github.com/python-arq/arq/tree/v0.28.0 — pessimistic execution, custom IDs, worker claim, timeout, result retention, and cancellation behavior.
- Redis persistence: https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/ — AOF replay and `appendfsync` loss windows.
- PyPI JSON metadata was checked on 2026-07-17 for all versions and dependency constraints listed above.

## Related Specs

- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/directory-structure.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/external-service-integrations.md`
- `.trellis/spec/backend/error-handling.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/guides/code-reuse-thinking-guide.md`

## Caveats / Not Found

- The repository currently has no API, worker, Redis client, LangGraph graph, job repository, or production persistence repository; their exact module names are not repository-established conventions.
- No live Redis/LLM integration was possible because neither service nor dependency is present in the current Compose/lock. Package APIs were verified against current official docs, published metadata, and tagged source.
- Exactly-once LLM execution is not achievable with generic OpenAI-compatible HTTP plus Redis checkpoints. The supported guarantee is at-least-once with completed-node checkpoint reuse.
- `appendfsync everysec` cannot support a literal “every acknowledged 202 survives sudden power loss” statement; that requires `appendfsync always` or managed durability with an explicit SLA.
- arq's job-ID uniqueness is not permanent deduplication. It lasts while the job/result key exists; stable application generation/state checks remain mandatory.
- LangGraph resumes existing threads against the graph code currently deployed. Persist a `graph_version` and reject/handle incompatible state migrations rather than assuming a changed state schema can always resume safely.
