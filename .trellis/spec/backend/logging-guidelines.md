# Logging Guidelines

## Scenario: Structured Application Logging

### 1. Scope / Trigger

Use this contract whenever application code emits logs or changes logging
sinks. Loguru is configured once by `data_agent.logging`; feature and
infrastructure modules reuse the exported `logger` and must not configure their
own sinks.

Call `setup_logging()` once from the owning process lifecycle before the first
application event. `data_agent.runtime` owns this call for both the FastAPI
lifespan and arq worker startup. Service, repository, graph, route, and entry
adapter modules never add sinks.

Every enabled console or file sink uses `enqueue=True`, so formatting,
serialization, terminal/file writes, rotation, and retention execute on
Loguru's queue consumer instead of an async caller's event-loop thread.

### 2. Configuration

Configuration comes from `app_config.logging`:

- `service_name: str`
- `deployment_environment: str`
- `console.enable: bool`, `console.level: str`
- `console.format: text | json`
- `file.enable: bool`, `file.level: str`
- `file.format: text | json`
- `file.path: Path`, `file.rotation: str`, `file.retention: str`

Sink rendering is explicit and is never inferred from the environment name.
The checked-in defaults use readable colored text on the console and one flat
JSON object per UTF-8 file line.

### 3. Canonical Event Schema

Every JSON record contains these flat fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `timestamp` | string | ISO-8601 UTC event time |
| `severity` | string | Loguru severity name |
| `message` | string | Concise Chinese human-readable description |
| `event_name` | string | Stable dot-separated application event |
| `service_name` | string | Configured service resource |
| `deployment_environment` | string | Configured deployment resource |
| `component` | string | Stable emitting application component |
| `trace_id` | string | Public job/request correlation ID or `-` |
| `logger_name` | string | Emitting Python module |
| `function_name` | string | Emitting Python function |
| `line_number` | integer | Emitting source line |
| `process_id` | integer | Emitting process |

Optional approved fields are bounded operational metadata:

```text
operation, outcome, node_name, job_status, attempt, revision,
question_round, duration_ms, table_count, column_count, metric_count,
question_count, rebuild_count, succeeded_count, failed_count,
error_code, error_type, stage, retryable, worker_role, stack_trace
```

Field keys use `snake_case`. Event names use stable dot-separated domain names.
Dynamic job IDs, source names, record IDs, revisions, and error types belong in
fields and must never be part of an event name or field name.

Missing application context uses safe fallbacks:
`event_name="application.log"`, `component="application"`, and
`trace_id="-"`. Application-owned call sites bind their real event and
component values.

### 4. Event Catalog and Levels

| Event name | Level | Purpose |
| --- | --- | --- |
| `application.lifecycle.started` | `INFO` | API or worker initialized |
| `application.lifecycle.stopped` | `INFO` | API or worker stopped |
| `ddl_metadata.job.accepted` | `INFO` | New DDL job durably accepted |
| `ddl_metadata.job.answers_submitted` | `INFO` | Answer revision accepted |
| `ddl_metadata.workflow.node.started` | `INFO` | Major workflow node began |
| `ddl_metadata.job.execution.completed` | `INFO`/`WARNING`/`ERROR` | Worker outcome |
| `ddl_metadata.job.retry_scheduled` | `WARNING` | Recoverable retry scheduled |
| `ddl_metadata.checkpoint.cleanup_deferred` | `WARNING` | Cleanup retry needed |
| `ddl_metadata.memory.payload_rebuild_failed` | `WARNING` | One rebuild item failed |
| `ddl_metadata.memory.index_initialization_deferred` | `WARNING` | A derived memory index could not initialize yet |

Use `INFO` for process lifecycle, major long-running node starts, and successful
business outcomes. Use `WARNING` for recoverable degradation and business
rejection. Use `ERROR` for terminal failures. Use `logger.exception()` or
`logger.opt(exception=error)` only inside an exception handler when an
unexpected handled exception needs a traceback. `diagnose=False` is mandatory
so local variables are not rendered.

Attempts, rounds, duration, counts, outcomes, and error metadata are fields on
the owning event. Do not emit noisy function entry/exit logs, duplicate Uvicorn
access logs, routine read-only request events, or a redundant start/success pair
when one outcome event carries the useful state.

### 5. Correlation and Concurrency

DDL jobs bind the stable public job ID as `trace_id` across API, graph, worker,
and cleanup records. Reuse the immutable bound logger within one operation:

```python
job_logger = logger.bind(
    trace_id=job_id,
    component="ddl_metadata.worker",
)
```

Never use `logger.configure(extra=...)` with a per-request or per-job value.
Process-wide defaults may contain service/environment and safe fallbacks only.
Independent bound loggers must be used for concurrent requests and jobs.

### 6. Security and Cardinality

Never log:

- passwords, API keys, access tokens, or complete connection URLs;
- raw DDL or source documents;
- prompts, user answers, memory content, full model responses, or hidden
  reasoning;
- arbitrary request bodies, exception locals, or unbounded payloads.

Structured tracebacks retain bounded frame information but omit the exception
message because transport exceptions may embed credentials or complete URLs.

Safe fields include public status transitions, revisions, attempts, rounds,
bounded counts, elapsed milliseconds, stable error codes, exception type, and
retryability. Large values must be replaced with approved bounded counts,
sizes, or hashes from the existing privacy contract.

### 7. Correct Usage

```python
from loguru import logger

logger.bind(
    trace_id=job_id,
    component="ddl_metadata.workflow",
    event_name="ddl_metadata.workflow.node.started",
    operation="persist_snapshot",
    outcome="started",
    node_name="persist_snapshot",
    attempt=attempt,
).info("开始持久化快照")
```

Incorrect usage embeds queryable data in the message or creates
high-cardinality event names:

```python
logger.info("node=persist_snapshot attempt={}", attempt)
logger.bind(event_name=f"ddl_metadata.job.{job_id}.failed").error("失败")
```

### 8. Validation and Error Matrix

| Condition | Required behavior |
| --- | --- |
| Missing or unknown YAML logging field | Pydantic rejects configuration during startup |
| Missing application context | Emit safe fallbacks; logging must not fail |
| Console or file sink disabled | Do not add that sink |
| Both sinks disabled | Complete setup without adding a sink |
| Invalid level, rotation, or retention | Let Loguru fail startup |
| File directory cannot be created | Let the filesystem error fail startup |
| Exception record | Remain one physical JSON line with bounded stack text |
| API or worker shutdown succeeds | Emit the final stopped event, then await `logger.complete()` |
| Startup fails after logging setup | Reverse-roll back completed resources, safely log rollback failures, and always await `logger.complete()` |
| One resource close fails during shutdown | Continue all closes, await `logger.complete()`, then propagate the original close error |
| Multiple resources fail during shutdown | Continue all closes, await `logger.complete()`, then raise an `ExceptionGroup` preserving every original error |
| Resource close raises cancellation | Continue all closes and await `logger.complete()`; preserve one cancellation unchanged or retain it in `BaseExceptionGroup` with other failures |

The logging unit tests must cover strict configuration, idempotent sink setup,
text rendering, per-line JSON parsing, canonical fields, typed application
fields, UTF-8, concurrent context isolation, queued sink registration,
deterministic completion before file reads/removal, lifecycle drain ordering,
close-failure propagation, and structured exceptions without diagnose
local-variable leakage. Logging tests must not use fixed sleeps to wait for
queued records.

When entry-point wiring changes, run `python -m data_agent.main` and inspect the
console and file outputs. When graph or worker logging changes, exercise an
interrupt/resume flow and verify no raw DDL, answers, prompts, model payloads,
credentials, or complete URLs appear.
