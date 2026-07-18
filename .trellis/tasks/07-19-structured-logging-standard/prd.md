# Standardize Application Log Structure and Usage

## Goal

Make application logs consistently searchable, correlatable, safe, and useful
for operational diagnosis without replacing Loguru or coupling the application
to one log-storage vendor.

## Background

- Loguru sink ownership is already centralized in
  `src/data_agent/logging.py:22`, with separate human-readable console and file
  formats at `src/data_agent/logging.py:9` and
  `src/data_agent/logging.py:15`.
- The existing contract requires a public job ID to be bound as `trace_id` and
  prohibits secrets, raw DDL, prompts, answers, model output, and unbounded
  content (`.trellis/spec/backend/logging-guidelines.md:42-48`).
- Current workflow logs encode operational data inside message strings such as
  `node=parse_ddl` and `attempt={}` rather than stable structured fields
  (`src/data_agent/ddl_metadata/workflow/graph.py:133-190`).
- The worker and memory payload rebuild paths also interpolate `error_type` and
  record identifiers into messages
  (`src/data_agent/ddl_metadata/worker.py:332` and
  `src/data_agent/ddl_metadata/memory/payloads.py:100`).
- Existing tests cover idempotent sink setup, default `trace_id`, explicit
  binding, and UTF-8 file output, but do not validate JSON records, field
  contracts, redaction, or exception structure
  (`tests/unit/infrastructure/test_logging.py:17-55`).

## Requirements

### R1. Environment-appropriate rendering

- Preserve concise, colorized, human-readable console output for local
  development.
- Provide one valid JSON object per production/file log record.
- Add an explicit `format: text | json` setting to each console and file sink.
- Default the console sink to text and the file sink to JSON; production
  deployments may select JSON console output without environment-name-based
  behavior.
- Preserve configurable sink enablement, level, path, rotation, and retention.

### R2. Stable event contract

- Every application event must carry stable machine-readable fields for
  timestamp, severity, message, event name, service, environment, component,
  and trace context.
- JSON records use a flat `snake_case` schema rather than nested application
  objects.
- Operation outcome events must use typed fields such as operation, outcome,
  duration, attempt/round, and bounded counts instead of embedding them in the
  message.
- Event names must be low-cardinality, stable, and contain no job IDs, source
  names, record IDs, or other occurrence-specific values.
- Field semantics should be straightforward to map to OpenTelemetry later,
  without requiring OpenTelemetry as part of this task.

### R3. Logging usage rules

- Human-readable messages remain concise Chinese descriptions; event names and
  field keys remain stable English identifiers.
- `INFO` records significant lifecycle or business outcomes, `WARNING`
  recoverable degradation, and `ERROR`/`exception` failed operations.
- Major long-running DDL workflow node starts remain at `INFO` so operators can
  identify the current stage in real time.
- Attempts, repair counts, and question rounds are fields on the owning node
  event rather than separate diagnostic messages.
- Worker success, waiting-input, rejection, and failure each produce a complete
  structured outcome event.
- Avoid noisy function-entry/function-exit logging and redundant start/success
  pairs when one completion event can contain the useful operational context.
- Exceptions must expose bounded structured error metadata and include a
  traceback only where the exception is being handled and diagnostic value
  requires it.

### R4. Correlation and context

- Preserve the public DDL job ID as the cross-component `trace_id`.
- Missing context must remain safe and must not make logging fail.
- Context binding must not leak between concurrent requests or jobs.

### R5. Security and cardinality

- Continue to prohibit secrets, credentials, complete connection URLs, raw DDL,
  prompts, user answers, full model responses, hidden reasoning, and unbounded
  payloads.
- Dynamic values belong in fields, never in event names or field names.
- Large values must be summarized with bounded counts, sizes, hashes approved
  by the existing privacy contract, or stable error codes.

### R6. Documentation and validation

- Update the backend logging guideline to define the authoritative schema,
  event naming, level selection, safe/unsafe fields, and examples.
- Add deterministic tests for JSON validity and required fields while
  preserving existing setup guarantees.
- Add focused tests for context isolation and structured exception records
  where the chosen implementation introduces those behaviors.

### R7. Complete current-call-site migration

- Migrate every existing application log call in the workflow graph, worker,
  and memory payload rebuild paths to the new event contract.
- Do not leave legacy queryable `key=value` fragments embedded only in message
  strings.

### R8. Fill high-value logging gaps

- Add low-noise structured events for service startup/shutdown, DDL job
  acceptance, answer submission, worker execution outcome, retries, and
  terminal failures.
- Do not add application logs for routine read-only query requests.
- Do not duplicate Uvicorn access logs or emit generic function entry/exit
  records.
- Boundary events must use bounded metadata and must not include raw request,
  DDL, answer, memory, prompt, or model-response content.

## Acceptance Criteria

- [x] Development console logs remain readable and contain trace context.
- [x] Production/file records are independently parseable JSON objects.
- [x] A representative successful DDL workflow event exposes event name,
      operation, outcome, trace ID, and bounded domain counts as fields.
- [x] A representative recoverable failure exposes stable error type/code fields
      without credentials, raw DDL, prompts, answers, or complete payloads.
- [x] Existing graph, worker, and payload-rebuild log call sites selected for
      migration no longer encode queryable values only inside message strings.
- [x] Job acceptance, answer submission, worker outcomes, retries/failures, and
      process lifecycle have low-noise structured events.
- [x] Routine read-only requests do not create redundant business events, and
      Uvicorn access logs are not duplicated.
- [x] Event names are stable and contain no occurrence-specific identifiers.
- [x] Missing trace context uses an explicit safe fallback.
- [x] Concurrent logging contexts do not leak trace IDs between jobs.
- [x] Repeated logging setup does not duplicate records.
- [x] Relevant Ruff, focused Pyright, compile, configuration, and focused pytest checks
      pass.

## Out of Scope

- Replacing Loguru with another logging framework.
- Deploying Elasticsearch, Loki, an OpenTelemetry Collector, or a hosted log
  backend.
- Adding distributed tracing or metrics instrumentation.
- Logging raw business or model payloads for debugging.
