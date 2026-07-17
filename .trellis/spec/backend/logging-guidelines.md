# Logging Guidelines

## Scenario: Application Logging

### 1. Scope / Trigger

Use this contract whenever application code emits logs or changes logging sinks. Loguru is configured once under `app/core`; business and client modules reuse the exported `logger` and must not configure their own sinks.

### 2. Signatures

```python
from app.core.logging import setup_logging

setup_logging(config: LoggingConfig = app_config.logging) -> None
logger.bind(trace_id="request-or-job-id").info("message")
```

Call `setup_logging()` once from the owning process lifecycle before the first
application log. The FastAPI lifespan and arq worker startup own this call;
service, repository, graph, and route modules never add sinks.

### 3. Contracts

Configuration comes from `app_config.logging`:

- `console.enable: bool`, `console.level: str`
- `file.enable: bool`, `file.level: str`
- `file.path: Path`, `file.rotation: str`, `file.retention: str`

Console lines contain millisecond time, level, source, `trace_id`, and message. File lines additionally contain the process ID and are written as UTF-8 to `<file.path>/data-agent.log`. Missing trace context renders as `trace_id=-`.

Log levels:

- `DEBUG`: temporary diagnostic detail useful during development.
- `INFO`: application lifecycle and successful significant operations.
- `WARNING`: recoverable degradation or unexpected input that was handled.
- `ERROR` / `exception`: failed operations; use `exception` inside an exception handler when the traceback is needed.

Never log passwords, API keys, access tokens, complete database or Redis URLs,
raw DDL, user answers, prompts, full model responses, hidden reasoning, or
unbounded request/document contents.

DDL jobs bind the stable public job ID as `trace_id` across graph, worker, and
persistence logs. Safe operational fields include graph node, public status
transition, revision/attempt/round, elapsed time, bounded object counts, stable
error code, and exception type. Do not use logical source names, DDL hashes, or
memory content as substitutes for `trace_id`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Missing or unknown YAML logging field | Pydantic rejects configuration during startup. |
| Missing `trace_id` | Emit `trace_id=-`; logging must not fail. |
| Console or file sink disabled | Do not add that sink. |
| Both sinks disabled | Complete setup without adding a sink. |
| Invalid level, rotation, or retention | Let Loguru raise its configuration error during startup. |
| File directory cannot be created | Let the filesystem error fail startup; do not silently discard file logs. |

### 5. Good / Base / Bad Cases

- Good: bind the request or job identifier once and reuse the returned contextual logger.
- Base: log without context; the default `trace_id=-` remains valid.
- Bad: call `logger.add()` in a business module, print secrets, or swallow sink setup errors.

### 6. Tests Required

The logging test must use a temporary directory and assert:

- calling `setup_logging()` twice does not duplicate a message;
- an unbound log contains `trace_id=-`;
- `logger.bind(trace_id="trace-1")` emits the bound ID;
- the configured file is created and readable as UTF-8.

When the format or entry-point wiring changes, also run `main.py` once and inspect both console and file output.
When graph or worker logging changes, exercise an interrupt/resume flow and
verify no raw DDL, answers, prompts, model payloads, credentials, or complete
URLs appear.

### 7. Wrong vs Correct

```python
# Wrong: local sink ownership and sensitive configuration output.
logger.add("client.log")
logger.info("Connecting to {}", app_config.mysql.url)

# Correct: central sink ownership and safe contextual fields.
from loguru import logger

request_logger = logger.bind(trace_id=trace_id)
request_logger.info("node=persist_snapshot table_count={}", table_count)
```
