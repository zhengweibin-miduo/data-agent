# LangGraph DDL Metadata Conversion Design

## 1. Scope and design stance

This design implements one local end-to-end feature: a browser submits MySQL
DDL, an asynchronous LangGraph workflow derives validated metadata and asks
the user for missing metric definitions, one MySQL transaction synchronizes
the accepted snapshot, and the browser can inspect and correct the resulting
long-term memory.

The implementation deliberately does not add authentication, a CLI, Celery, a
MySQL job table, an ORM layer, a generic model-provider abstraction, or
foreign-key target persistence. Those concerns are not required by the first
release.

## 2. Architecture

```text
Local browser
    |
    | HTTP JSON
    v
FastAPI routes
    |
    +--> Redis job/status store --> arq queue --> worker
                                         |
                                         v
                              compiled LangGraph workflow
                                |       |        |
                                |       |        +--> Redis checkpoint
                                |       +----------> OpenAI-compatible LLM
                                +------------------> SQLGlot parser
                                         |
                                         v
                         MetaRepository + MemoryRepository
                                         |
                                         v
                      one MysqlClientManager session
                        |                       |
                        v                       v
                 Meta default DB       application memory DB
```

### Responsibility boundaries

- **API routes** own HTTP validation, status-code mapping, CORS, and response
  serialization. They do not contain graph or persistence logic.
- **Job service/store** owns the public job state machine, execution revision,
  answer compare-and-set, retention, and queue submission.
- **Worker** owns one execution attempt and translates retryable failures into
  bounded queue retries.
- **LangGraph** owns deterministic/LLM processing order, checkpointed state,
  semantic correction loops, and human interrupts.
- **DDL parser** owns physical schema facts. It never calls the LLM.
- **Memory service/repository** owns compatible-memory retrieval, bounded
  context projection, payload rebuild, browser management, state/relations,
  and supersession.
- **Meta repository** owns the four-table snapshot statements; the service
  commits Meta plus accepted long-term memories in one transaction.
- **Memory repository** uses schema-qualified tables in the configured
  application database (`data_agent` by default). It does not use the Meta
  database and does not create a second engine or Session.
- Existing manager patterns own long-lived Redis, checkpoint, chat-model, and
  MySQL client lifecycles. Package `__init__.py` files remain side-effect free.

Feature services are grouped under `app/service/ddl_metadata/`, persistence
under `app/repository/ddl_metadata/`, and the worker entry point is
`app/worker/ddl_metadata.py`. Shared contracts remain in
`app/model/ddl_metadata.py`, shared clients remain in `app/client/`, and the
FastAPI boundary remains in `app/api/app.py`.

No layer receives a FastAPI request object, Redis connection, SQLAlchemy
Session, or chat-model client through graph state. Graph state contains only
serializable typed data.

## 3. API contract

### Submit a job

```http
POST /api/v1/metadata/ddl-jobs
Content-Type: application/json
```

```json
{
  "source": "local_dw",
  "dialect": "mysql",
  "ddl": "CREATE TABLE ..."
}
```

`source` is required because stable IDs must not collide when two logical data
sources contain the same qualified table name. `dialect` is a literal
`"mysql"` in the first release.

Successful submission returns:

```http
202 Accepted
```

```json
{
  "job_id": "019f...",
  "status": "pending",
  "status_url": "/api/v1/metadata/ddl-jobs/019f..."
}
```

The API validates encoded byte size before queueing. Initial configurable
limits are 256 KiB of DDL, 50 tables, and 500 columns. Exceeding a limit is a
business rejection, not a worker retry.

### Query a job

```http
GET /api/v1/metadata/ddl-jobs/{job_id}
```

The response always uses the same typed envelope:

```json
{
  "job_id": "019f...",
  "status": "waiting_input",
  "revision": 1,
  "question_set_id": "sha256:...",
  "questions": [],
  "result": null,
  "error": null,
  "created_at": "2026-07-17T15:30:00+08:00",
  "expires_at": "2026-07-17T16:00:00+08:00"
}
```

Only fields relevant to the current status are populated. Unknown or
retention-expired IDs return `404`.

### Answer metric questions

```http
POST /api/v1/metadata/ddl-jobs/{job_id}/answers
```

```json
{
  "revision": 1,
  "question_set_id": "sha256:...",
  "answers": [
    {"question_id": "average_order_amount.definition", "answer": "..."}
  ]
}
```

The job store atomically verifies `waiting_input`, the current revision, the
question-set ID, and the 30-minute deadline. A valid submission changes the
job to `pending`, increments the execution revision, and adds a resume
activation to the Redis dispatch outbox. Repeating the same answer payload is
idempotent; conflicting or stale submissions return `409`. An expired round
returns `410` after transitioning the job to `rejected`.

Redis unavailability during submit/resume returns `503`; no job is reported as
accepted unless the atomic job-record plus dispatch-outbox write succeeds.

### Manage long-term memory

The browser-facing memory API is deliberately smaller than Memos. It exposes
only operations required to audit and correct LLM reuse:

```http
GET /api/v1/metadata/memories?source=local_dw&kind=METRIC_DEFINITION&row_status=NORMAL&pinned=true&limit=50&cursor=...
GET /api/v1/metadata/memories/{memory_uid}
PATCH /api/v1/metadata/memories/{memory_uid}
POST /api/v1/metadata/memories/{memory_uid}/corrections
```

The list requires `source`, defaults `row_status` to `NORMAL`, orders by
`updated_at DESC, id DESC`, caps `limit` at 100, and uses an opaque cursor. Its
items contain identity, kind, scope, fingerprint, status, pinned flag, safe
summary, and timestamps. Detail additionally returns canonical `content`,
derived `payload`, and batch-loaded typed relations.

`PATCH` accepts exactly one management change:

```json
{"pinned": true}
```

or:

```json
{"row_status": "ARCHIVED"}
```

Pin/unpin and archive are idempotent. Archived memories cannot be pinned and
are excluded from default graph retrieval. Hard delete and arbitrary content
patching are not exposed.

A correction accepts the current kind's structured content only:

```json
{
  "content": {
    "kind": "METRIC_DEFINITION",
    "definition": "..."
  }
}
```

Only active `SEMANTIC_DECISION` and `METRIC_DEFINITION` records are
correctable. The API fixes `source`, `kind`, `scope_key`, and
`schema_fingerprint` from the target memory, validates the replacement through
the current discriminated Pydantic contract, and verifies referenced Meta
object IDs. It then creates a user-confirmed memory, adds `SUPERSEDES`, and
archives the old memory in one MySQL transaction. `METRIC_QUESTION` and
`USER_ANSWER` are immutable audit records.

Correction does not patch the current Meta snapshot. The response is:

```http
201 Created
```

```json
{
  "memory_uid": "7f3c...",
  "supersedes_uid": "2a91...",
  "requires_reprocess": true
}
```

The next DDL job for that source loads the correction, revalidates it against
the current AST, and applies it through the normal all-table transaction. This
keeps manual memory management from creating a partial or unvalidated Meta
state.

Mutation routes briefly acquire the same logical-source lease used by jobs.
If a job owns it, they return `409 source_busy`; this prevents archive, pin, or
correction from racing with a graph that already loaded memory. Unknown UIDs
return `404`; invalid content or filters return `422`; immutable, archived, or
otherwise conflicting targets return `409`.

## 4. Public job state machine

```text
pending -> running
running -> waiting_input | succeeded | rejected | pending(retry) | failed
waiting_input -> pending(answer) | rejected(timeout)
succeeded | rejected | failed -> terminal
```

A single job-store transition function owns this table and performs
revision-aware compare-and-set updates. API routes and workers must not update
individual status fields directly.

The stable public `job_id` identifies the workflow. Queue execution IDs include
the revision (`{job_id}:{revision}`), so a completed interrupt attempt does not
block a later resume attempt and duplicate resume requests cannot enqueue the
same revision twice.

Terminal job summaries remain queryable for 24 hours. `waiting_input` allows
30 minutes per round. A small scheduled cleanup job marks expired waits as
`rejected`, removes their graph checkpoints, and retains only the safe terminal
summary until normal result expiry.

## 5. Typed workflow state

The state is a Pydantic/typed mapping containing only JSON-serializable values:

```python
class DdlGraphState(TypedDict):
    job_id: str
    source: str
    dialect: Literal["mysql"]
    ddl: str
    canonical_ddl: str | None
    ddl_hash: str | None
    physical_schema: PhysicalSchema | None
    semantic_metadata: SemanticMetadata | None
    metric_questions: list[MetricQuestion]
    metric_answers: list[MetricAnswer]
    metrics: list[MetricMetadata]
    validation_errors: list[ValidationIssue]
    semantic_attempts: int
    metric_attempts: int
    question_round: int
    status: JobStatus
```

Raw clients, database sessions, exception objects, and loggers are never
checkpointed. Errors use stable codes plus safe structured fields.

## 6. Graph

```text
START
  |
parse_ddl
  |
load_and_validate_memory
  |
classify_metadata  <--------------------+
  |                                     |
validate_metadata -- repairable once ---+
  | valid
plan_metric_questions
  | no fact/metric need --------------------------+
  | questions                                     |
await_metric_answers (interrupt)                  |
  | resume                                        |
generate_metrics <----------------------+         |
  |                                      |         |
validate_metrics -- technical repair ---+         |
  |                                      |         |
  +-- missing business meaning and round < 2       |
  |              -> plan_metric_questions          |
  |                                                |
  +-- valid ---------------------------------------+
  |
build_memory_candidates
  |
persist_snapshot
  |
SUCCEEDED

Any terminal validation branch -> REJECTED
Retryable infrastructure exception -> worker retry from checkpoint
Exhausted/non-retryable infrastructure exception -> FAILED
```

### `parse_ddl`

1. Parse with SQLGlot using the MySQL dialect.
2. Require every non-empty statement to be `CREATE TABLE`.
3. Extract exact schema/table/column names, normalized types, constraints, and
   comments into `PhysicalSchema`.
4. Derive primary-key and declared foreign-key roles deterministically.
5. Produce a canonical schema representation and SHA-256 DDL hash.
6. Reject duplicate qualified tables/columns and configured size/count limits.

The parser never executes input SQL. Unsupported/malformed input is rejected
before any model call.

### `load_and_validate_memory`

Load only `NORMAL` memories matching the current `source`, object scope, and
schema fingerprint. Load their typed relations in one batch. Prefer pinned
user-confirmed metric/semantic decisions over model-only decisions.

Every canonical memory document is parsed through the current Pydantic type and
checked against current AST-owned table/column IDs. A compatible memory becomes
bounded evidence for the classifier or may satisfy an unchanged decision. A
stale fingerprint, unknown object, invalid payload, conflicting active memory,
or incompatible content version is never allowed to override the DDL:

- rebuild a stale derived payload from canonical content when possible;
- ignore and report corrupt/incompatible memory;
- route conflicting active user decisions to clarification instead of choosing
  one silently;
- re-run the model only for missing or changed semantic decisions.

The node passes a small typed memory capsule to later nodes, not raw memory
history. Retrieval is exact SQL in the first release; no vector search is
required.

### `classify_metadata`

The model receives compact physical-schema JSON, not the raw DDL. Naming,
constraint, type, and comment evidence is included. Related tables are grouped
by the parsed foreign-key graph; independent bounded groups may use LangGraph
`Send` with configured concurrency.

The structured response can supply only:

- table role: `fact` or `dim`;
- role for non-PK/non-declared-FK columns: `measure` or `dimension`;
- description and aliases;
- confidence and evidence references.

It cannot add, remove, rename, or retype physical objects. Deterministic IDs
are assigned outside the model. Model temperature is zero.

Use Pydantic `with_structured_output`. Default to native `json_schema`; expose
an explicit `function_calling` setting for compatible servers that do not
implement OpenAI Structured Outputs. A startup/live capability check must fail
clearly when the configured method is unsupported. Never silently fall back to
unconstrained text or `json_mode`.

### `validate_metadata`

Validation requires exact table/column set equality with the parser output,
valid enums, immutable structural roles, evidence that references known
objects, and a configured semantic confidence threshold. It supports factless
fact tables; a table is not rejected merely for lacking a numeric measure.

Schema/format/hallucination errors loop once to `classify_metadata` with the
specific validation issues. A second invalid response or low-confidence
semantic ambiguity is rejected.

### `plan_metric_questions` and `await_metric_answers`

Dimension-only imports may proceed with an empty metric set. For fact tables,
the model asks only for missing business facts needed by the current schema,
such as:

- metric business name and purpose;
- aggregation or distinct-count rule;
- numerator/denominator where applicable;
- filters and inclusion/exclusion rules;
- time grain/window and unit;
- the relevant validated columns.

Questions are a typed list with stable question IDs and target fact/column IDs.
The question-set hash and round number prevent stale answers.

`await_metric_answers` calls LangGraph `interrupt()` with that payload. The
worker records `waiting_input` and returns without occupying a worker slot.
The answer endpoint queues a new revision that resumes the same graph thread
with `Command(resume=...)`.

At most two question rounds are permitted. The second incomplete result or a
30-minute timeout is rejected without writing Meta rows.

LangGraph restarts an interrupted node from its beginning when resumed.
Therefore the interrupt node performs no mutation, queue write, database write,
or other non-idempotent side effect before `interrupt()`.

### `generate_metrics` and `validate_metrics`

The model sees only validated metadata, the exact question set, and user
answers. Structured metric output includes name, aliases, a complete business
definition, and relevant column IDs.

The validator requires:

- unique metric identities within the logical source/fact scope;
- every relevant column to exist in the validated current snapshot;
- at least one `column_metric` association per metric;
- no claims not supported by the user's answers;
- a description that records the supplied calculation, filters, time grain,
  and unit when those concepts apply.

Technical schema/reference errors may loop once to `generate_metrics` with
validation feedback. Missing business meaning returns to question planning
only if a clarification round remains. It is never filled by guessing.

### `persist_snapshot`

Stable 64-character hexadecimal IDs fit the existing schema:

```text
table_id  = sha256("table\0"  + source + "\0" + qualified_table_name)
column_id = sha256("column\0" + table_id + "\0" + column_name)
metric_id = sha256("metric\0" + source + "\0" + fact_table_id + "\0"
                   + normalized_metric_name)
memory_uid = sha256("memory\0" + source + "\0" + memory_kind + "\0"
                    + scope_key + "\0" + schema_fingerprint + "\0"
                    + content_hash)
```

One `MysqlClientManager.session()` transaction:

1. Loads existing columns and column/metric links for submitted table IDs.
2. Upserts `table_info`, `column_info`, and the current `metric_info`.
3. Upserts the current `column_metric` links.
4. Deletes stale links in the submitted scope.
5. Deletes stale columns in the submitted scope.
6. Deletes metrics that became unreferenced after scoped cleanup.
7. Upserts the accepted memory records and typed relations.
8. Archives older active memories superseded by the accepted decisions.

Static bound SQL or SQLAlchemy Core statements are owned by the Meta and memory
repositories, which share the same managed Session. Table/column names never
come from unbound input. Missing tables outside the submitted DDL are not
queried for deletion.

The four Meta tables remain unqualified and therefore use the default database
from `mysql.url`. `llm_memory` and `llm_memory_relation` are qualified with
`memory.database`. That database name is a strict MySQL identifier, must differ
from the URL's default database, and still runs through the same engine,
connection, Session, and InnoDB transaction.

The MySQL transaction rolls back on every exception. If commit succeeds but
the worker dies before the post-node checkpoint, rerunning this node produces
the same snapshot because IDs, memory UIDs, unique relations, and cleanup scope
are deterministic.

## 7. Long-term LLM memory adapted from Memos

The design borrows four concrete patterns from `usememos/memos` without
copying its product model:

1. one durable record with stable UID, state, timestamps, canonical content,
   pinning, and a JSON payload;
2. derived tags/properties in the payload are rebuildable from canonical
   content;
3. typed relations live in a separate table with a unique triple;
4. archived records leave the default active query without destroying audit
   history.

Memos is a note application, not an LLM memory engine. This project adapts the
patterns into typed semantic memory rather than storing arbitrary Markdown.
It also does not copy Memos' service-level multi-step create behavior: accepted
Meta rows, memories, and relations must remain one transaction here because a
partially created semantic memory would be unsafe LLM context.

### Memory schema

These tables live in the configured application database, not in Meta:

```text
<memory.database>.llm_memory
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY
  uid                 CHAR(64) UNIQUE
  source              VARCHAR(128)
  kind                VARCHAR(32)
  scope_key           VARCHAR(256)
  schema_fingerprint  CHAR(64)
  row_status          VARCHAR(16)       # NORMAL / ARCHIVED
  pinned              BOOLEAN
  content             JSON              # canonical typed decision
  payload             JSON              # rebuildable retrieval projection
  content_version     VARCHAR(32)
  created_at          DATETIME
  updated_at          DATETIME

<memory.database>.llm_memory_relation
  memory_id          BIGINT
  related_memory_id  BIGINT
  relation_type      VARCHAR(32)         # REFERENCE / COMMENT / SUPERSEDES
  UNIQUE(memory_id, related_memory_id, relation_type)
```

`content` contains only explicit, validated application facts: decision,
concise evidence, relevant object IDs, user-confirmed metric definition, and
safe provenance. It never contains hidden chain-of-thought, raw prompts, or
unbounded model transcripts.

`payload` is a derived projection containing tags, trust level, object
identities, schema fingerprint, model/prompt/graph versions, and fields needed
for bounded retrieval. A payload version change runs a rebuild from `content`;
payload is never the sole copy of accepted meaning.

Memory kinds are limited to real consumers:

- `SEMANTIC_DECISION`;
- `METRIC_QUESTION`;
- `USER_ANSWER`;
- `METRIC_DEFINITION`.

Operational stack traces and retry logs are not semantic memories. Recovery
attempt counts and final graph version may be stored as safe provenance, while
full diagnostics stay in job state/logs.

### Relations and lifecycle

- `COMMENT`: a user answer belongs to a generated metric question.
- `REFERENCE`: a semantic/metric decision depends on specific table, column,
  question, or answer memories.
- `SUPERSEDES`: a new accepted decision replaces an older active decision.

User-confirmed metric definitions are pinned and receive the highest retrieval
trust. Model-only semantic decisions remain unpinned and must still validate
against the current AST.

Correction is append/supersede, not in-place historical rewriting: create the
new deterministic memory, add `SUPERSEDES`, and archive the previous memory in
the same transaction. Normal retrieval excludes `ARCHIVED`; audit queries may
include it. Hard deletion is reserved for explicit retention cleanup and must
remove inbound/outbound relations safely.

Browser correction follows the same lifecycle but does not directly edit Meta.
It is a user-confirmed input for the next fully validated DDL run. Pin/archive
mutations change only retrieval state and are serialized with jobs by the
logical-source lease.

### Retrieval and reuse

1. Query by `source`, `scope_key`, `schema_fingerprint`, kind, and
   `row_status=NORMAL`.
2. Batch-load all relations for the selected memory IDs to avoid N+1 queries.
3. Validate canonical content against current Pydantic contracts and AST IDs.
4. Rank pinned user-confirmed memory before compatible model-only memory.
5. Pass only the bounded relevant capsule to the LLM.
6. Reuse unchanged accepted decisions; call the LLM and ask the user only for
   missing or structurally changed meaning.

If two active user-confirmed memories conflict, the workflow asks the user or
rejects; it never resolves the conflict by timestamp alone.

### Payload rebuild and recovery

A small batch runner mirrors Memos' rebuildable-payload approach:

- read canonical memories in batches of 100;
- rebuild tags/properties/fingerprints with the current extractor;
- update only payload/version fields;
- log one row's safe error and continue the batch;
- report processed/succeeded/failed counts for rerun.

If payload rebuild fails, canonical content remains authoritative and the
memory is excluded from automatic reuse until repaired. Failed, rejected,
expired, or incomplete graph candidates never become `NORMAL` long-term
memory.

For consistency, permit only one active job per logical `source` in the first
release. The Redis job service owns that source lease across
`waiting_input`, renews it, and releases it on every terminal transition. This
prevents an older job from committing stale memory after a newer job. A
per-table concurrency scheme can replace this ceiling only when measured
throughput requires it.

Qdrant is not part of the first memory path. If semantic search is later
needed, index `NORMAL` memory summaries with stable UIDs as a rebuildable
projection; MySQL remains the source of truth.

Source evidence is pinned to `usememos/memos` commit
`469c995cc04b5e7de259156d28c58b948e85d111`; the task research artifact records
the exact files and URLs used so future Memos changes do not silently alter this
design.

## 8. Redis and worker topology

- Use `arq`, not Celery, for the lightweight async worker and bounded retry.
- Use the LangGraph Redis checkpointer for durable graph state with
  `thread_id = job_id`.
- Initialize the async checkpointer with its required async setup call before
  graph use, and invoke the graph with synchronous checkpoint durability so a
  completed step is durable before the next step starts.
- Use the same Redis deployment for queue, job projection, and checkpoints,
  with separate key prefixes.
- The local Compose service uses Redis 8 because the checkpoint package needs
  RedisJSON and RediSearch capabilities. It uses a named volume, AOF
  persistence, a health check, and loopback-published port.
- Lock the Python Redis client to the intersection supported by the selected
  checkpoint package and `arq 0.28`: `redis>=5.2.1,<6`.
- API and worker processes initialize/close their own managed async clients in
  their lifecycle hooks; they do not share process-local connection objects.

The API process may restart without affecting queued work. A worker retry uses
the same `thread_id`; LangGraph resumes after the last completed checkpoint.
Redis downtime pauses submission and execution. Once Redis recovers, persisted
queue/checkpoint data is processed. Loss of the Redis volume is outside the
local first-release durability guarantee.

`arq` provides at-least-once execution. Graceful worker cancellation requeues
work; after a hard process loss, reclaim waits for the in-progress key to
expire (the configured job timeout plus its safety margin). Queue handlers and
every graph node with a side effect must therefore be idempotent.

### Redis records and dispatch outbox

Do not expose `arq` result keys as the API record. Keep three application-owned
structures:

```text
ddl:job:{job_id}  -> status, generation, attempt, deadlines, safe result/error,
                     question payload, graph_version
ddl:dispatch      -> sorted-set outbox of job_id:generation activations
ddl:waiting       -> sorted-set of answer deadline -> job_id:generation
```

Submission writes the job record and generation `0` dispatch item in one Redis
transaction before returning `202`. A startup/periodic dispatcher enqueues the
deterministic arq ID `ddl:{job_id}:{generation}` and removes the outbox item
only after enqueue succeeds or the same arq ID already exists. This closes the
API-crash window between accepting and queueing a job.

Answer submission uses one Lua script (or equivalent `WATCH`/`MULTI` loop) to
check status, generation, question-set ID, payload hash, and deadline; store
the answer; increment the generation; move the job to `pending`; remove the
waiting entry; and add the new dispatch item. Worker/public-state transitions
use the same generation guards, so stale activations are harmless.

Set `keep_result=0` for arq jobs because the application record owns results.
Persist `graph_version` and fail safely instead of resuming a checkpoint whose
state schema is incompatible with the deployed graph.

### Recovery reconciliation

At worker entry, inspect both the public job generation and the latest graph
checkpoint:

1. Return for terminal or correctly projected `waiting_input` jobs.
2. With no checkpoint, invoke the graph using the stored initial request.
3. With an interrupt plus an answer for this generation, resume with
   `Command(resume=answer)`.
4. With an interrupt but no answer, repair the public status to
   `waiting_input` without invoking the graph.
5. With no next graph node, project the checkpointed terminal result into the
   public job record.
6. Otherwise invoke with `None` to continue from the latest checkpoint.

This repairs crashes between a durable graph checkpoint and the public Redis
status projection without replaying the initial input.

## 9. Failure and recovery matrix

| Failure | Classification | Required behavior |
|---|---|---|
| Malformed/unsupported DDL | `rejected` | No LLM call, no MySQL write |
| Input/table/column limit exceeded | `rejected` | Return stable limit error |
| Structured-output/schema error | semantic repair | Feed exact validation issues back once |
| Hallucinated object/role conflict | semantic repair then `rejected` | Never persist an unknown object |
| Low-confidence table/column meaning | `rejected` | Do not guess |
| Incomplete metric answer, round 1 | `waiting_input` | Ask one follow-up set |
| Incomplete metric answer, round 2 | `rejected` | No MySQL write |
| User does not answer for 30 minutes | `rejected` | Cleanup checkpoint, retain safe summary |
| Stale/incompatible memory | cache miss/revalidation | Ignore for automatic reuse; current AST remains authoritative |
| Corrupt canonical memory | memory warning | Exclude, log safe UID/error, regenerate current decision |
| Derived payload rebuild failure | recoverable maintenance error | Preserve canonical content, exclude until rebuilt, continue other rows |
| Conflicting active user memories | business ambiguity | Ask user or reject; never choose by timestamp alone |
| Duplicate/stale answer | HTTP `409` | Do not enqueue another revision |
| LLM timeout/rate limit/5xx | retryable system error | Bounded backoff from prior checkpoint |
| LLM authentication/config error | `failed` | No retry; safe error without secret |
| Crash after LLM response but before node checkpoint | at-least-once caveat | LLM request may repeat; generic compatible APIs offer no shared exactly-once guarantee |
| Redis unavailable at submission | HTTP `503` | Do not claim job acceptance |
| Redis unavailable during work | paused/retryable | Preserve AOF-backed state and continue after recovery |
| Graceful worker shutdown | retryable | `arq` requeues; resume the same graph thread |
| Hard worker crash | retryable with delay | Reclaim after in-progress TTL; resume the same graph thread |
| MySQL connection/deadlock failure | retryable | Roll back Meta plus memory and rerun `persist_snapshot` only |
| MySQL schema/config error | `failed` | Roll back, no blind retry |
| Crash after MySQL commit | retryable/idempotent | Re-run scoped Meta/memory snapshot and unique relations safely |
| API process restart | no job-state change | Browser continues polling same job ID |

Retry scheduling is bounded and uses jittered backoff. The worker records the
original exception type internally but returns only stable safe error data.

With local `appendfsync everysec`, a sudden host/power loss can lose roughly
one second of acknowledged Redis writes. Normal API/worker/Redis process
restarts are recoverable. If every returned `202` must survive abrupt host
loss, switch to `appendfsync always` or managed Redis durability; deliberate
volume deletion and `FLUSHALL` are never recoverable.

## 10. Configuration and secrets

Extend the strict Pydantic/YAML configuration with:

- `api.host`, `api.port`, and `api.cors_origins`;
- `api.max_ddl_bytes`, `api.max_tables`, and `api.max_columns`;
- `redis.url`, key prefix, checkpoint/result retention, and worker concurrency;
- `llm.base_url`, `llm.model`, request timeout, semantic confidence threshold,
  structured-output method, and max concurrency;
- `memory.content_version`, `memory.payload_version`, source-lease timeout, and
  rebuild batch size;
- `memory.database`, a strict MySQL identifier that defaults to `data_agent`
  and must differ from the default database in `mysql.url`.

`DATA_AGENT_LLM_API_KEY` is the only model secret and is read from the
environment. It is never placed in YAML, Redis job responses, checkpoints,
logs, or frontend code.

## 11. Logging and observability

`setup_logging()` remains centralized in `app/core`. API and worker entry
points call it once. Every accepted job binds `job_id` as Loguru `trace_id`.

Log state transitions, node name, attempt/round number, elapsed time, safe
counts, and error code. Do not log raw DDL, answers, prompts, model responses,
tokens, API keys, database URLs, or Redis URLs containing credentials.

## 12. Compatibility, rollout, and rollback

- Add bounded compatible dependency lines: `langgraph>=1.2,<1.3`,
  `langgraph-checkpoint-redis>=0.5,<0.6`,
  `langchain-openai>=1.3,<1.4`, `arq>=0.28,<0.29`,
  `redis>=5.2.1,<6`, plus bounded FastAPI/Uvicorn and SQLGlot versions verified
  by the lockfile.
- Add Redis to local Compose and add an idempotent application-database
  bootstrap for `llm_memory` plus `llm_memory_relation`; keep the Meta
  bootstrap limited to its four business tables.
- Keep current MySQL, Qdrant, Elasticsearch, and TEI managers/configuration
  compatible.
- Update backend specs after the new API/service/repository/worker patterns
  actually exist.
- Rollback stops API/worker and removes the new local Redis service/config.
  A failed job cannot require data rollback because it writes in one
  transaction. Successfully synchronized Meta and memory rows are intentional
  business state and are not automatically reverted by code rollback.

## 13. Validation strategy

- Parser/validator checks use the repository's sample `dw.sql` plus focused
  malformed, unsupported, factless-fact, hallucination, and limit cases.
- Graph checks use a deterministic fake chat model and count model calls.
- Redis integration checks cover queueing, interrupt/resume, stale answer
  rejection, timeout cleanup, checkpoint resume, and worker retry.
- MySQL integration checks cover four-table atomicity, repeat idempotency,
  scoped stale cleanup, orphan metric cleanup, memory supersession/relations,
  schema separation, and forced cross-database rollback of Meta plus memory.
- Memory checks cover exact compatible retrieval, user-confirmed precedence,
  archived exclusion, conflict handling, payload rebuild continuation, and
  unchanged-object reuse without bypassing AST validation.
- Memory API checks cover bounded list/detail projection, filters/cursors,
  idempotent pin/archive, immutable-kind rejection, atomic correction and
  supersession, source-lease conflicts, secret/prompt exclusion, and the
  `requires_reprocess` contract.
- Recovery checks force `persist_snapshot` to fail once and assert the resumed
  run does not call completed LLM nodes again.
- API checks cover `202`, status projection, answer revision conflicts,
  memory management, `404/409/410/422/503`, loopback defaults, and CORS
  allowlisting.
- CI keeps existing lock, Ruff, Pyright, compile, configuration, and client
  checks and adds focused module checks plus local Redis service coverage.
