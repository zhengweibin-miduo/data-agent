# Logging Guidelines

## Scenario: AOP Application Logging

### 1. Scope / Trigger

Use this contract whenever application code emits logs or an execution entry
establishes logging context. Loguru is configured once by
`data_agent.logging`; feature and infrastructure modules never configure sinks.

Business code owns only the level and the complete Chinese message:

```python
logger.warning("对话长期记忆提炼延后，任务将在退避后自动重试")
```

Business log calls must not use `logger.bind()`, `logger.contextualize()`,
`logger.opt()`, logging Policy / Classifier / Outcome objects, or structured
field arguments. Time, source location, request/job correlation, component,
operation and safe exception metadata are injected by Loguru records and AOP
execution boundaries.

### 2. Configuration

Configuration comes from `app_config.logging`:

- `service_name: str`
- `deployment_environment: str`
- `console.enable: bool`, `console.level: str`
- `console.format: text | json`
- `file.enable: bool`, `file.level: str`
- `file.format: text | json`
- `file.path: Path`, `file.rotation: str`, `file.retention: str`

Call `setup_logging()` once from the owning process lifecycle. Every enabled
sink uses `enqueue=True`; shutdown must await `logger.complete()`.

### 3. Record Ownership

Loguru records and the formatter provide:

```text
timestamp, severity, message, logger_name, function_name, line_number,
process_id, service_name, deployment_environment
```

AOP execution boundaries provide bounded context when available:

```text
trace_id, component, operation, request_id, job_id, task_id, node_name,
attempt, revision, worker_role
```

The record patcher detects an active `except` context without requiring
`logger.opt(exception=...)`. It adds `error_type`; ERROR/CRITICAL records may
also contain a bounded stack trace whose exception message and local variables
are omitted.

Missing context uses safe fallbacks such as `trace_id="-"`,
`component="application"` and `operation="-"`. A missing or malformed context
must never make the business log call fail.

### 4. Levels and Messages

- `INFO`: process lifecycle, major long operation starts and successful
  business outcomes.
- `WARNING`: recoverable degradation, deferred work and business rejection.
- `ERROR`: terminal system failure.

Messages must identify the business action, result and next behavior. A short
message such as `"失败"` or `"延后"` is not sufficient. When a safe business
error code is already part of the domain result, include it in the message.
Runtime exception class and stack metadata are supplied by AOP and must not be
manually formatted into the message.

Do not emit noisy generic function entry/exit pairs, duplicate Uvicorn access
logs, or routine read-only events with no operational value.

### 5. AOP Boundaries

`logging_context()` is an infrastructure primitive backed by `ContextVar`.
It merges an immutable context and resets the exact token in `finally`.
Never mutate per-request or per-job data through global
`logger.configure(extra=...)`.

`logging_boundary()` preserves the original callable behavior while applying
context for the real execution lifetime. Its `component`, `operation`, and
`context_factory` arguments are all optional. With no arguments, component and
operation come from the wrapped callable module and qualified name. Context
comes from `inspect.signature()` binding plus an exact field allowlist read only
from direct parameters, mappings, declared Pydantic fields, and dataclass
fields. Reflection must not inspect the call stack, local variables, arbitrary
attributes, or properties.

Business classes, functions, and route handlers never carry
`@logging_boundary` and never call `logging_context()` directly. Weave wrappers
only where the application registers execution with a framework:

- FastAPI middleware covers the complete request and streamed response.
- application composition wraps process lifespans and SSE generators;
- `WorkerSettings` wraps arq functions, cron callbacks, and lifecycle hooks;
- LangGraph nodes are wrapped at each `graph.add_node()` registration;
- Async-generator context is active during iteration, not only when the
  generator object is created.
- Nested boundaries inherit outer values and override only their own fields.

When no execution boundary supplies component or operation, the Loguru record
patcher derives them from the record module and function before formatting.

Wrappers must preserve the original return value, exception object,
`CancelledError`, `GeneratorExit`, `arq.Retry`, function signature and
generator semantics. Context extraction and record patching are observability
enhancements; their own failure must not affect business execution.

### 6. Security and Cardinality

Never log:

- passwords, API keys, access tokens or complete connection URLs;
- raw DDL, source documents, prompts, user answers or memory content;
- full model responses, hidden reasoning, request bodies or exception locals;
- unbounded collections or arbitrary objects.

Safe metadata includes public IDs, statuses, revisions, attempts, bounded
counts, stable error codes, exception class names and retryability. Stack traces
must be bounded and replace the exception message with a fixed omission marker.

### 7. Correct Usage

Correct business code:

```python
try:
    await extractor.dispatch()
except Exception:
    logger.warning("对话长期记忆提炼延后，任务将在退避后自动重试")
```

The surrounding execution boundary supplies component, operation, trace and
the active exception type.

Incorrect business code:

```python
logger.bind(
    trace_id=lease_token,
    component="conversation.extraction",
    operation="extract_conversation_memory",
    error_type=type(error).__name__,
).warning("提炼延后")
```

### 8. Validation Matrix

| Condition | Required behavior |
| --- | --- |
| Plain `logger.warning(message)` inside a boundary | Inherit boundary context |
| Nested boundary exits | Restore the exact outer context |
| Concurrent requests/jobs | Never exchange trace or operation fields |
| Warning inside `except` | Add safe `error_type`, without stack trace |
| Error inside `except` | Add safe `error_type` and bounded sanitized stack |
| Async generator streams after route return | Retain context during iteration |
| Context factory or patcher fails | Preserve business return/exception semantics |
| API or worker shutdown succeeds | Emit final message, then await `logger.complete()` |

Logging tests must cover JSON/text rendering, context defaults, nested reset,
async concurrency isolation, exception sanitization, sync/async/generator
wrappers, HTTP streaming scope, queued sink completion and shutdown ordering.
Run Ruff, Pyright, non-integration tests, compileall and settings loading after
changing logging infrastructure or AOP wiring.
