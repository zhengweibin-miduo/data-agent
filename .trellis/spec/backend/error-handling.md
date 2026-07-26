# Error Handling

## Current Strategy

Configuration and low-level client errors normally propagate unchanged.
Lifecycle misuse still raises an actionable `RuntimeError`. The root
`data_agent.errors` module defines one stable safe application error,
`DataAgentError`, for business rejection and API/worker projection:

```python
DataAgentError(
    code,
    stage,
    message,
    retryable=False,
    http_status=422,
    details=None,
)
```

`code`, `stage`, retryability, and safe bounded details may cross process or
HTTP boundaries. The exception message is internal and must not contain raw
DDL, answers, prompts, secrets, or full service URLs.

`details` defaults to an empty dict and never back-fills `message`. The API error
handler and the public job event stream both project `details` verbatim, so
back-filling would publish every internal message that a caller forgot to
sanitize. Only pass `details` when the keys are deliberately public — bounded
identifier lists, counts, or an exception class name — and let `message` reach
logs only.

## Client Lifecycle Errors

Every infrastructure wrapper's `get_client()` rejects access before
initialization. For example, `data_agent.infrastructure.mysql` uses this
shape:

```python
if cls._client is None:
    raise RuntimeError(
        "MySQL 客户端尚未初始化，请先调用 MySQLDatabase.initialize()"
    )
```

`QdrantClient`, `ElasticsearchClient`, and `TEIEmbeddingClient` follow the same
pattern with service-specific messages. Keep the message actionable and
preserve the concrete wrapper name.

Closing an uninitialized or already closed wrapper is deliberately harmless.
Most wrappers use this shape:

```python
if cls._client is None:
    return
```

MySQL additionally clears its engine and Session-factory references before
awaiting disposal so a concurrent replacement is not erased. In every wrapper,
a later `initialize()` creates a fresh resource after close.

## Propagation and Cleanup

- `AppSettings.from_yaml()` lets file, YAML, and Pydantic validation errors
  propagate. `SettingsModel` uses `extra="forbid"`, so unknown keys are errors
  rather than silently ignored values.
- Client initialization and request failures are not wrapped in generic
  exceptions. The original SQLAlchemy, Elasticsearch, Qdrant, Hugging Face, or
  transport exception remains available to the caller.
- `MySQLDatabase.session()` commits on normal exit; on any
  `BaseException`, it rolls back and re-raises the same exception. Session
  closure is owned by the async context manager.
- Live integration fixtures acquire the managed client and close it in
  `finally`; keep cleanup independent of assertion or request success.
- `CheckpointStore.initialize()` closes its partially entered saver if
  Redis index setup fails; API and worker lifecycles close initialized clients
  in reverse ownership order.
- `MetadataSnapshotService.persist()` lets the original SQLAlchemy/asyncmy exception
  escape so the worker can classify transient failures while the managed
  Session rolls back Meta and memory together.

## API Error Responses

The application exception handler centrally maps `DataAgentError` raised by
the split `data_agent.ddl_metadata.api.jobs` and `.memories` routers to its
declared status and a safe envelope:

```json
{
  "error": {
    "code": "stale_answer",
    "stage": "waiting_input",
    "retryable": false,
    "details": {}
  }
}
```

Configured mappings include:

- unknown jobs/memories: `404`;
- stale answers, source leases, immutable/deleted conflicts: `409`;
- expired answers: `410`;
- request/Pydantic validation, unknown/duplicate answer IDs, and business input
  rejection: `422`;
- Redis transport failure at the API boundary: `503`.

FastAPI owns its standard `422` request-validation response. Do not expose
exception reprs, stack traces, internal Redis fields, raw DDL, or model output.

Conversation APIs reuse the same safe business envelope. Tenant mismatch and
unknown conversation/memory identifiers both return `404`;
`conversation_busy`, stale turns, and idempotency content conflicts return
`409`. Empty, oversized, unknown, attachment, multimodal, or unsupported-role
payloads remain FastAPI/Pydantic `422` errors. Extraction failures are worker
retries and never hide or roll back already committed messages.

## Worker Retry and Terminal Errors

The worker retries only the explicit transient exception set: OpenAI
connection/timeout/rate-limit/5xx errors, SQLAlchemy `OperationalError`, Redis
connection failures, and ordinary connection/timeouts. It transitions
`running -> pending` before raising arq `Retry` with bounded exponential
backoff and jitter.

Business validation/rejection is graph state, not an infrastructure retry.
After retry exhaustion or a non-retryable exception, the worker writes a safe
`JobError`, moves the public job to `failed`, and deletes the graph checkpoint.
Unknown exceptions expose only `error_type`; the original traceback remains in
server logs. Revision-aware Redis transitions prevent a stale activation from
overwriting a newer or terminal public state.

Waiting-input expiry is an explicit transition, not passive TTL. The answer
script and periodic sweep both check the deadline and revision so only one can
win; terminal job summaries then receive the configured retention TTL.
Every terminal transition also removes raw DDL, answers, current questions,
question-set hashes, and deadline fields from the internal Redis Hash; only the
safe public summary fields remain for retention. The same atomic transition
adds a checkpoint-cleanup outbox item; periodic cleanup acknowledges it only
after thread deletion succeeds.

## Scenario: Local Asynchronous DDL Metadata API

### 1. Scope / Trigger

Use this contract when changing the local FastAPI routes, job transitions,
metric answers, browser memory management, CORS, or safe error projection.

### 2. Signatures

```http
POST /api/v1/metadata/ddl-jobs
GET /api/v1/metadata/ddl-jobs/{job_id}
GET /api/v1/metadata/ddl-jobs/{job_id}/events
POST /api/v1/metadata/ddl-jobs/{job_id}/answers
GET /api/v1/metadata/memories/search
GET /api/v1/metadata/memories/{memory_uid}
GET /api/v1/metadata/memories/{memory_uid}/history
PATCH /api/v1/metadata/memories/{memory_uid}
DELETE /api/v1/metadata/memories/{memory_uid}
```

### 3. Contracts

- Submit returns `202` only after the Redis job record, source lease, and
  dispatch outbox are durable.
- After acceptance is durable, submit and answer-submit also dispatch that one
  activation immediately, so an interactive round does not wait out a
  `dispatch_pending` cron tick. The outbox member stays the only recoverable
  scheduling request: immediate dispatch is best-effort, its failure only falls
  back to the cron, and it must never undo an acceptance already promised.
  Deterministic arq job ids make the immediate path and the cron idempotent with
  respect to each other.
- Build the arq client for that path directly rather than with
  `arq.create_pool`: `create_pool` connects during startup with retry backoff,
  which escalates "Redis briefly unreachable" from per-request failures into a
  process that cannot start, and makes the lifespan untestable without Redis.
  arq needs byte responses, so it cannot share the application's
  `decode_responses=True` client.
- `graph_version` stays out of the public projection. It only tells the worker
  whether a job may still be interpreted by the current graph; callers have no
  use for it, and the worker reads it through an internal accessor.
- Accepting an answer clears `questions_json` so a `pending` read stops
  returning the previous round's questions. It must keep `question_set_id` and
  `answer_hash`: the idempotent-replay verdict matches on them, and clearing
  them would turn a legitimate answer retry into `stale_answer`.
- Attempt counting happens inside the transition script via `HINCRBY`. A caller
  that reads the old value and writes back `value + 1` loses the increment if
  another actor interleaves; the single-active-actor assumption that currently
  saves it is not enforced by anything.
- Before submission encodes DDL as UTF-8, reject `len(ddl) >
  api.max_ddl_bytes`; every Unicode code point occupies at least one UTF-8
  byte. Encode only the bounded remainder and apply the exact byte comparison
  so multibyte input near the limit retains the same
  `DataAgentError(code="ddl_too_large", stage="submit")` business response.
- Status exposes only the stable public state, safe result/error, current
  bounded questions, revision, and expiry.
- The SSE route resolves the job and current Stream tail before starting the
  response. Unknown jobs retain the existing `404` projection and Redis
  failures retain the existing safe `503` projection.
- After an SSE response starts, Redis/read/contract failures emit one fixed
  `stream_error` event containing only a stable code, stage, retryability, and
  the last public job coordinates, then close so the client can reconnect.
- Every SSE connection starts with an authoritative `snapshot`. Terminal
  snapshots and terminal events close the connection; `waiting_input` remains
  open across answer-driven revisions. Idle periods use SSE comment
  heartbeats, not business events.
- Answer requires the current revision and question-set ID; its question IDs
  must exactly match the current set.
- Memory search requires source and a bounded query, caps result size, and
  returns only MySQL-rechecked authoritative content.
- PATCH appends a user-confirmed update event and reports
  `requires_reprocess=true`; DELETE is an audited soft delete. Neither silently
  patches current Meta.
- Default host is `127.0.0.1`; configured CORS origins must resolve to local
  browser origins. Authentication and non-loopback deployment are unsupported.

### 4. Validation & Error Matrix

| Condition | HTTP/result |
|---|---|
| Accepted submit | `202 pending` with opaque job ID |
| Unknown job or memory | `404` |
| Stale answer, active source lease, immutable/deleted conflict | `409` |
| Answer deadline expired | `410 rejected` and checkpoint cleanup scheduled |
| Invalid DDL, payload, filter, or answer IDs | `422` with safe error code |
| Redis unavailable during submit/resume | `503`; never claim acceptance |
| Redis unavailable before SSE starts | `503` with fixed `redis_unavailable` envelope |
| Redis unavailable after SSE starts | Safe `stream_error` event, then close |
| Valid memory update | `200`, event ID, `requires_reprocess=true` |

### 5. Good / Base / Bad Cases

- Good: the browser polls an accepted job, answers the exact current question
  set, and receives a terminal safe projection after atomic persistence.
- Base: an expired answer loses the race to the deadline transition and gets
  `410` without resuming the graph.
- Bad: a route edits Redis fields or Meta rows directly, enables wildcard
  CORS, or returns raw exception/model content.

### 6. Tests Required

```powershell
uv run pytest tests/integration/test_api.py
uv run pytest tests/unit/ddl_metadata/test_job_events.py
uv run pytest tests/unit/ddl_metadata/test_job_events_api.py
uv run pytest tests/integration/test_worker.py
uv run pytest tests/integration/test_ddl_metadata_flow.py
```

Tests must assert `202/404/409/410/422/503`, answer compare-and-set, timeout
cleanup, bounded memory projection, update reprocessing, loopback defaults,
and rejection of non-local CORS origins.

### 7. Wrong vs Correct

```python
# Wrong: route-level state mutation bypasses revision, lease, and outboxes.
await redis.hset(f"ddl:job:{job_id}", mapping={"status": "pending"})

# Correct: routes delegate the atomic transition to DDLJobStore.
job = await job_store.submit_answers(job_id, request)
```

## Common Mistakes

- Do not return `None` from `get_client()` when initialization was forgotten;
  fail with the established `RuntimeError`.
- Do not swallow configuration or transport exceptions with a broad
  `except Exception`.
- Do not swallow an exception raised inside a managed MySQL Session; rollback
  and preserve the original failure.
- Do not skip async cleanup after a live integration assertion fails.
- Do not add connection side effects to package `__init__.py` files; lifecycle
  remains explicit through infrastructure wrapper methods.
- Do not catch broad exceptions in repositories or graph nodes merely to return
  `None`; preserve transaction rollback and worker classification.
- Do not mark validation ambiguity retryable or translate it into `failed`;
  deterministic/model business rejection ends as `rejected`.
- Do not let API routes update Redis Hash fields directly; use `DDLJobStore` so
  transition, revision, lease, outbox, and retention rules remain atomic.
