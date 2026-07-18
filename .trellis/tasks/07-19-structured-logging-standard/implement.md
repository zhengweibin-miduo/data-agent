# Structured Application Logging Implementation Plan

## 1. Configuration and Schema

- [x] Extend logging settings with process-wide service/environment context and
      explicit `text | json` format selection for both sinks.
- [x] Update `conf/app_config.yaml` to keep console text and switch file output
      to JSON.
- [x] Define the reserved canonical fields and safe fallback values centrally
      in `src/data_agent/logging.py`.

## 2. Rendering

- [x] Preserve the developer text renderer and add stable event/component
      visibility.
- [x] Implement a callable flat JSON formatter that emits exactly one UTF-8
      JSON object per physical line.
- [x] Serialize supported bound fields with stable types and prevent internal
      formatter state from leaking into output.
- [x] Preserve `diagnose=False`, rotation, retention, sink enablement, and
      idempotent setup.

## 3. Existing Call-Site Migration

- [x] Convert every workflow graph log to
      `ddl_metadata.workflow.node.started` with structured `node_name`,
      `attempt`, and `question_round` fields where applicable.
- [x] Convert checkpoint cleanup degradation to a structured warning.
- [x] Respect the concurrent removal of the legacy memory payload rebuild
      implementation; do not restore its obsolete warning call.
- [x] Confirm no task-owned application log leaves queryable values only inside
      the message string.

## 4. Missing Boundary Events

- [x] Add API process startup/shutdown events after successful initialization
      and during orderly shutdown.
- [x] Add job acceptance and answer-submission events without logging request
      bodies, raw DDL, or answers.
- [x] Add worker process startup/shutdown events.
- [x] Add worker execution outcome and retry/failure events at actual state
      transition boundaries.
- [x] Avoid read-only request logs and avoid duplicating Uvicorn access logs.

## 5. Tests

- [x] Expand logging unit tests for strict config, text output, JSON parsing,
      required canonical fields, typed application fields, UTF-8, and repeated
      setup.
- [x] Prove two independently bound concurrent loggers do not leak trace IDs.
- [x] Cover structured exception output without `diagnose` local-variable
      leakage.
- [x] Update focused API/worker/graph behavior without making tests depend on
      timestamps or source line numbers.
- [x] Use `check_equal`, `check_condition`, or `fail_check`; add no bare
      assertions.

## 6. Documentation and Spec

- [x] Update `.trellis/spec/backend/logging-guidelines.md` with the canonical
      schema, event catalog, naming/cardinality rules, level guidance,
      safe/unsafe fields, and correct examples.
- [x] Keep the spec in English and runtime/test Docstrings in Chinese.

## 7. Validation

- [x] `uv lock --check`
- [x] `uv run ruff check src tests`
- [ ] `uv run pyright src tests` — one unrelated concurrent Mem0 test error
      remains at `tests/integration/persistence/test_metadata_repository.py:126`;
      focused task Pyright passes.
- [x] `uv run python -m compileall -q src tests`
- [x] `uv run python -m data_agent.settings`
- [x] `uv run pytest -m "not integration"`
- [x] Run focused logging, API, graph, and worker tests during iteration.
- [x] `git diff --check`
- [x] Inspect a real text console record and a parsed JSON file record.

## 8. Review and Rollback Gates

- [x] Verify no raw DDL, answers, prompts, model payloads, credentials, or
      complete URLs appear in changed log calls or fixtures.
- [x] Verify event names contain no occurrence-specific values.
- [x] Verify the working tree's unrelated pre-existing changes are untouched.
- [x] Verify formatter behavior preserves one-line JSON and safe exception
      rendering; rollback was not required.
