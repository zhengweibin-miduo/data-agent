# Structured Application Logging Design

## 1. Scope and Boundaries

This change keeps Loguru as the logging implementation and keeps
`data_agent.logging` as the only sink owner. It standardizes rendering,
application event fields, and call-site behavior across:

- API and worker process lifecycle;
- DDL job acceptance and answer submission;
- workflow node progress;
- worker outcomes, retries, and terminal failures;
- checkpoint cleanup degradation;
- memory payload rebuild degradation.

It does not add a log backend, intercept or duplicate Uvicorn access logs, or
introduce OpenTelemetry runtime dependencies.

## 2. Configuration Contract

Add `format: Literal["text", "json"]` to both `ConsoleLoggingSettings` and
`FileLoggingSettings`.

Default configuration:

```yaml
logging:
  service_name: data-agent
  deployment_environment: local
  console:
    enable: true
    level: INFO
    format: text
  file:
    enable: true
    level: INFO
    format: json
```

`service_name` and `deployment_environment` are logging resource context and
are configured once. Sink behavior is explicit and never inferred from an
environment name.

## 3. Canonical Flat JSON Schema

Every JSON record contains:

| Field | Type | Source |
| --- | --- | --- |
| `timestamp` | ISO-8601 UTC string | Loguru record time |
| `severity` | string | Loguru level |
| `message` | string | Rendered human-readable message |
| `event_name` | string | Bound application event identifier |
| `service_name` | string | Logging configuration |
| `deployment_environment` | string | Logging configuration |
| `component` | string | Bound application component |
| `trace_id` | string | Public job/request correlation ID or `-` |
| `logger_name` | string | Emitting module |
| `function_name` | string | Emitting function |
| `line_number` | integer | Emitting source line |
| `process_id` | integer | Emitting process |

Optional flat fields describe the occurrence, for example:

```text
operation
outcome
node_name
job_status
attempt
question_round
duration_ms
table_count
column_count
error_code
error_type
retryable
stack_trace
```

Fields use `snake_case`. Event names use stable dot-separated domain names.
Dynamic values are forbidden in event names and field names.

The schema maps cleanly to OpenTelemetry later:

- `timestamp` -> Timestamp
- `severity` -> SeverityText
- `message` -> Body
- `event_name` -> EventName
- `trace_id` -> TraceId
- service/environment fields -> Resource
- remaining occurrence fields -> Attributes

## 4. Rendering

`setup_logging()` installs each sink according to its explicit format:

- text: existing colorized developer rendering, extended with stable event and
  component context;
- JSON: a callable formatter builds the canonical dictionary and serializes it
  as exactly one UTF-8 JSON line.

A callable JSON formatter is required instead of Loguru `serialize=True`
because the built-in serialization nests application fields under Loguru's
record object and does not match the agreed flat schema.

The formatter:

1. derives standard metadata from the Loguru record;
2. consumes reserved application keys from `record["extra"]`;
3. appends remaining approved scalar/list fields without nesting;
4. serializes exceptions into bounded structured fields while keeping the
   physical log record on one line;
5. never mutates global correlation context during an event call.

Unknown or absent standard application context uses explicit safe defaults
(`event_name="application.log"`, `component="application"`, `trace_id="-"`).
Application-owned call sites must bind real event and component values.

## 5. Context and Concurrency

Use immutable bound Loguru logger instances for job/request context:

```python
job_logger = logger.bind(
    trace_id=job_id,
    component="ddl_metadata.worker",
)
```

Do not globally call `logger.configure(extra=...)` with a per-request or
per-job identifier. A bound logger can be reused within one operation and does
not leak its `trace_id` into another concurrent task.

Global defaults contain only process-wide resource context and safe fallback
values.

## 6. Event Catalog

The initial catalog is deliberately small:

| Event name | Level | Purpose |
| --- | --- | --- |
| `application.lifecycle.started` | INFO | API or worker initialized |
| `application.lifecycle.stopped` | INFO | API or worker shut down |
| `ddl_metadata.job.accepted` | INFO | New job accepted |
| `ddl_metadata.job.answers_submitted` | INFO | Answer revision accepted |
| `ddl_metadata.workflow.node.started` | INFO | Major workflow node began |
| `ddl_metadata.job.execution.completed` | INFO/WARNING/ERROR | Worker outcome |
| `ddl_metadata.job.retry_scheduled` | WARNING | Recoverable retry scheduled |
| `ddl_metadata.checkpoint.cleanup_deferred` | WARNING | Cleanup retry needed |
| `ddl_metadata.memory.payload_rebuild_failed` | WARNING | One bounded rebuild item failed |

The same event name must always retain compatible field semantics. Outcome
differences belong in `outcome`, `job_status`, `error_code`, and `retryable`.

## 7. Call-Site Migration

All existing `logger.bind(...).info/warning(...)` calls are migrated.
Queryable `node=`, `attempt=`, `round=`, `uid=`, and `error_type=` fragments
move into fields. Messages become concise Chinese descriptions.

New boundary records are added only where they describe a significant state
change. Read-only API queries, ordinary functions, and existing Uvicorn access
records are not duplicated.

## 8. Safety

Approved fields are bounded operational metadata only. The implementation must
not bind:

- raw DDL or source documents;
- prompts, answers, memory content, or model responses;
- API keys, tokens, passwords, or complete connection URLs;
- hidden reasoning;
- arbitrary request bodies or exception local variables.

`diagnose=False` remains mandatory. Error events prefer stable error codes,
exception types, stage, retryability, and bounded counts. Tracebacks are
included only for unexpected handled exceptions where the call site uses
`logger.exception`; they remain a JSON string so one record stays on one
physical line.

## 9. Compatibility and Rollback

- Existing sink enablement, levels, paths, rotation, retention, and UTF-8
  behavior remain supported.
- Configuration is intentionally strict: missing new `format`,
  `service_name`, or `deployment_environment` values fail validation until the
  checked-in YAML and tests are migrated together.
- Rollback is a single coherent revert of settings, formatter, config, tests,
  call sites, and logging guideline.
- No persisted business data or external service schema changes are involved.
