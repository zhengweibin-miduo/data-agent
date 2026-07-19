# Asyncify Runtime Blocking Boundaries

## Goal

Audit the production runtime for synchronous work that can materially block
FastAPI, arq, or LangGraph event loops; convert justified boundaries to
asynchronous execution, remove the superseded synchronous public paths, and
keep the repository internally consistent.

## Background

- The project already uses asynchronous clients for MySQL (`AsyncSession` over
  `asyncmy`), Redis (`redis.asyncio`), Elasticsearch, Qdrant, TEI, the
  OpenAI-compatible LLM, and LangGraph Redis checkpoints.
- `DDLWorkflowNodes.parse_node()` currently calls the synchronous public
  `parse_ddl()` API directly inside an async LangGraph node. The configured
  request boundary permits up to 256 KiB, 50 tables, and 500 columns, so
  SQLGlot parsing can monopolize the event-loop thread.
- Loguru console and file sinks are currently registered without queued
  delivery. Every runtime log call can therefore format and write on the
  caller's event-loop thread.
- The DDL request model has no ingress length limit. `DDLJobStore.submit()`
  currently performs a complete UTF-8 encoding before enforcing the 256 KiB
  byte limit, so an arbitrarily large decoded JSON string can consume the
  event-loop thread before rejection.
- Configuration YAML is loaded synchronously once during module import, and
  client constructors are synchronous object construction without network
  I/O. Pure identifiers, routing, validation, serialization, and domain
  projection functions are deterministic CPU work.

## Requirements

- R1. Inventory every production synchronous function and every synchronous
  call reachable from an async runtime entry point, classifying it as blocking
  I/O, material CPU work, startup-only work, or small deterministic work.
- R2. Convert the DDL parsing runtime boundary so the async LangGraph node does
  not execute SQLGlot parsing on the event-loop thread.
- R3. Remove the superseded synchronous public DDL parsing API and migrate all
  production and test consumers to the final async contract. A private
  synchronous callable may exist only as the implementation submitted to a
  worker thread; it must not remain an application-facing alternative.
- R4. Make production log delivery non-blocking for async callers while
  preserving configured formats, structured fields, rotation, retention,
  exception redaction, and shutdown flushing.
- R5. Bound the pre-parser DDL size check on the event-loop thread without
  changing the existing `DDLMetadataError(code="ddl_too_large")` projection:
  reject a character count already above the byte limit before encoding, then
  perform the exact UTF-8 byte check only on the bounded remainder.
- R6. Do not convert small pure functions or startup-only object construction
  to coroutines merely for naming consistency. Each retained synchronous
  boundary must have a recorded technical reason.
- R7. Preserve HTTP paths and response contracts, arq registrations and job
  names, Redis/MySQL/LangGraph persisted contracts, configuration keys,
  logging field names, and error behavior.
- R8. Delete retired synchronous tests, fixtures, imports, and compatibility
  wrappers after the async migration; do not keep duplicate public sync and
  async paths.
- R9. Update affected project specifications with executable async signatures,
  lifecycle requirements, error behavior, and required tests.

## Acceptance Criteria

- [x] AC1. A repository-wide audit records every production module containing
  synchronous functions and gives an evidence-backed convert/retain decision.
- [x] AC2. The LangGraph parsing node awaits an async parsing contract whose
  SQLGlot work runs outside the event-loop thread; concurrency tests prove the
  loop can make progress while parsing is in flight.
- [x] AC3. Active production code and tests contain no import or call of the
  retired synchronous public `parse_ddl()` contract.
- [x] AC4. Production Loguru sinks use queued delivery, and API/worker shutdown
  awaits log completion before process resources are considered closed.
- [x] AC5. Logging tests wait for queued records deterministically and verify
  JSON/text formatting, trace isolation, exception redaction, and shutdown
  flushing without timing sleeps.
- [x] AC6. Retained synchronous functions are pure, bounded, or startup-only;
  no synchronous database, Redis, HTTP, embedding, LLM, checkpoint, or file
  write remains on an async request/job hot path.
- [x] AC7. Oversized DDL is rejected with the existing safe business error
  before unbounded UTF-8 encoding, while multibyte input near the limit still
  receives the exact byte-count decision.
- [x] AC8. `uv lock --check`, Ruff, Pyright, `compileall`, configuration
  loading, all tests, Docker Compose validation, and `git diff --check` pass.
- [x] AC9. A stale-contract search confirms the replaced synchronous public
  paths and duplicate tests/helpers were deleted rather than retained as
  compatibility aliases.

## Out of Scope

- Making every `def` an `async def`.
- Offloading small validation, hashing, Pydantic conversion, routing, or
  in-memory collection transforms without evidence that they block the loop.
- Changing external API payloads, persistence schemas, queue/checkpoint
  formats, or configuration keys.
- Adding a generic executor abstraction before more than one concrete runtime
  boundary requires it.
