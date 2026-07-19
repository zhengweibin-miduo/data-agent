# Implementation Plan

## 1. Migrate the DDL Parser Boundary

- [x] Move the complete current parser body to private `_parse_ddl_sync()`.
- [x] Add the public async `parse_ddl()` wrapper using `asyncio.to_thread()`.
- [x] Await the parser from the LangGraph parsing node.
- [x] Convert all direct parser tests and consumers to native async calls.
- [x] Add a deterministic synchronization test proving SQLGlot executes on a
  worker thread while the event loop progresses.
- [x] Preserve all existing parser success and error assertions.

Validation:

```powershell
uv run pytest tests/unit/ddl_metadata/test_parsing.py
uv run pytest tests/unit/ddl_metadata/workflow/test_graph.py
rg -n "parse_ddl\\(" src tests
```

Rollback point: parser contract and consumers.

## 2. Bound Pre-Parser Job Submission Work

- [x] Add a character-count fast rejection before UTF-8 encoding.
- [x] Retain the precise byte-count check for the bounded remainder.
- [x] Add ASCII and multibyte boundary tests for the existing error contract.
- [x] Confirm no Pydantic/HTTP error projection changes.

Validation:

```powershell
uv run pytest tests/unit/ddl_metadata/jobs tests/integration/test_api.py
```

Rollback point: job-ingress size guard.

## 3. Queue and Drain Log Delivery

- [x] Register every enabled console/file sink with `enqueue=True`.
- [x] Await `logger.complete()` after the final API and worker stopped event.
- [x] Guarantee drain on close failure without replacing the original error.
- [x] Convert file-reading logging tests to async and await completion before
  reads, handler removal, and temporary-directory cleanup.
- [x] Add sink option and lifecycle ordering/failure tests.

Validation:

```powershell
uv run pytest tests/unit/infrastructure/test_logging.py
uv run pytest tests/integration/test_api.py tests/integration/test_worker.py
```

Rollback point: queued logging and lifecycle flush.

## 4. Specification and Consistency Pass

- [x] Record the async parser signature and private worker-thread rule.
- [x] Record queued sink and shutdown-drain requirements.
- [x] Record the bounded two-stage DDL size check.
- [x] Search source/tests/current specs for retired synchronous parser calls,
  aliases, immediate queued-file assumptions, and unawaited completion.
- [x] Confirm the research inventory still matches final production code.

## 5. Quality and Review Gates

- [x] Dispatch `trellis-check` for a full-scope review under `code_review.md`.
- [x] Run:

```powershell
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv run pytest -m "not tei"
uv run pytest tests/integration/infrastructure/test_tei_embeddings.py
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

- [x] Audit PRD AC1-AC9 and report exact verification results.
- [x] Leave the unrelated SSE task and its files untouched.
