# Runtime assembly evidence

## Current entry points

- `src/data_agent/application.py:27-65` owns API startup, state publication,
  reverse shutdown, lifecycle logging, and Loguru draining.
- `src/data_agent/ddl_metadata/worker/lifecycle.py:36-104` repeats the shared
  resource lifecycle and additionally owns derived-index setup, LLM capability,
  CheckpointStore, graph construction, maintenance, and worker state publication.
- `src/data_agent/application.py:31-39` publishes `jobs`, `memories`, and
  `conversations` on `app.state`.
- `src/data_agent/ddl_metadata/worker/lifecycle.py:64-73` publishes `jobs`,
  `conversation_extractor`, and `graph` on the arq context.

## Observable lifecycle contracts

- `tests/unit/infrastructure/test_logging_lifecycle.py:116-177` fixes API close
  order as TEI, Qdrant, Elasticsearch, MySQL, Redis, stopped event, and
  `logger.complete()`.
- `tests/unit/infrastructure/test_logging_lifecycle.py:180-237` fixes Worker
  close order as CheckpointStore, LLM, TEI, Qdrant, Elasticsearch, MySQL,
  Redis, stopped event, and `logger.complete()`.
- The current startup paths do not roll back resources when a later initializer
  fails. The user approved changing this behavior to reverse rollback while
  preserving the original startup exception.
- The current shutdown stops after the first close failure. The user approved
  best-effort closing, preserving a single original error and using
  `ExceptionGroup` for multiple errors.

## Architecture findings

- Runtime assembly is an in-process dependency category. Existing concrete
  infrastructure wrappers remain internal implementation details; no new
  single-adapter ports are justified.
- `MemoryRepository`, `MemoryIndexOutboxRepository`, and `MemoryService` pass
  the deletion test and remain unchanged.
- The selected design uses a private ordered action registry, a public
  `start(role, target)` / `stop(handle)` interface, and an explicit
  `RuntimeHandle`.

