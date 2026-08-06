---
goal: Implement a bounded natural-language-to-read-only-SQL query flow
version: 1.0
date_created: 2026-08-06
last_updated: 2026-08-06
owner: zwb
status: 'Implemented'
tags: [feature, backend, query, sql, security]
---

# Introduction

![Status: Implemented](https://img.shields.io/badge/status-Implemented-success)

Implement a `query` bounded context that reuses the current DDL, Meta Projection, Answer Readiness, Conversation and LLM infrastructure to generate, validate and automatically execute one bounded read-only DW query. Keep the existing `chat-turns` contract unchanged and expose the new flow through `POST /api/v1/conversations/{conversation_uid}/query-turns`.

## 1. Requirements & Constraints

- **REQ-001**: Accept a user question only with the current `source + MySQL DDL` context, parse that DDL deterministically and scope every recalled or generated object to its stable table/column IDs.
- **REQ-002**: Reuse one existing Meta semantic search for table, column and metric candidates; invoke value search only after candidate column IDs are known.
- **REQ-003**: Convert the question to a strict `QueryIntent` whose quoted phrases come only from user messages, ground every required slot to one authoritative Meta object, and return one highest-impact clarification question whenever grounding is missing or ambiguous.
- **REQ-004**: Validate one MySQL `SELECT`/`WITH ... SELECT` through SQLGlot, an object allowlist and a database `EXPLAIN`; allow one repair attempt and then fail closed.
- **REQ-005**: Automatically execute a validated query without user confirmation only after Answer Readiness approves the AST-resolved target tables.
- **REQ-006**: Generate a strict `QueryDraft` containing SQL, bound parameters and referenced table/column/metric IDs; never execute model output directly.
- **REQ-007**: Preserve the complete business result. Keep explicit Top-N semantics and return detail results as NDJSON streaming batches of at most 500 rows or 1 MiB, while preserving Conversation turn idempotency and bounded application memory.
- **SEC-001**: Use a dedicated `mysql+asyncmy` DW query URL whose database user has `SELECT` only; do not reuse the writable application session.
- **SEC-002**: Reject multiple statements, non-query AST nodes, non-DW/system schemas, unknown tables/columns, `SELECT *`, cross joins, unsupported join edges, raw predicate literals, dangerous functions, user variables and file output.
- **SEC-003**: Enforce a 10-second execution budget and a 500-row/1-MiB transport batch budget without imposing a total-result limit; never retry model generation for timeout, permission, connection or readiness failures.
- **SEC-004**: Emit structured audit fields for identity, SQL hash, referenced table IDs, duration, row count and outcome; do not log bound parameter values or result rows.
- **CON-001**: MVP queries the unified DW across all sources. Source-specific row filtering is excluded because current DW fact tables do not contain a source column.
- **CON-002**: Keep `ddl_metadata.parsing.parse_ddl` DDL-only; implement query SQL parsing and validation in the new query domain.
- **CON-003**: Do not add a search engine, keyword extractor, reranker, relation persistence table, metric-expression schema or audit table.
- **CON-004**: Never use model confidence or a silent product default to resolve ambiguous metrics, time ranges, dimensions or filters; only a unique authoritative candidate or an explicit stored user rule can bypass clarification.
- **GUD-001**: Follow `.trellis/spec/backend/` dependency direction: domain is pure, application depends on ports, adapters own LLM/MySQL/FastAPI conversion, and `backend/src/application.py` is the composition root.
- **PAT-001**: Expose one deep `QueryApplication.stream(QueryRequest) -> AsyncIterator[QueryEvent]` interface; callers and tests must not orchestrate recall, repair, readiness or execution steps themselves.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Establish typed query contracts, deterministic scope and context retrieval.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add `backend/src/query/domain.py` with `QueryIntent`, exact user-quote evidence, ambiguity/clarification models, `QueryDraft`, `ValidatedQuery`, `QueryContext`, stable validation issue codes and pure SQLGlot validation helpers. Require one statement, SELECT/CTE root, `dw` object allowlist, parameter parity and safe joins without adding a fixed business-result limit. | | |
| TASK-002 | Add `backend/src/query/application/contracts.py` with intent parser, planner, Meta retrieval, readiness and executor ports plus `QueryRequest` and `clarification/metadata/rows/complete` NDJSON event contracts. Keep infrastructure types out of these interfaces. | | |
| TASK-003 | Add a Query-side Meta adapter that calls existing `MetadataSearchService.search_metadata` once, filters candidate IDs against `parse_ddl` output, expands metric column IDs, then calls `search_values` only for retained column IDs. Build FK context from `PhysicalSchema.relationships`. | | |
| TASK-004 | Add focused tests under `backend/tests/unit/query/test_context.py` proving exact user-quote validation, unique grounding, one-at-a-time clarification order, Conversation-based clarification continuation, cross-source candidate rejection, field-before-value ordering, metric column expansion, FK closure and `complete=false` value semantics. | | |

### Implementation Phase 2

- **GOAL-002**: Generate and validate bounded SQL with one repair.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Add `backend/src/query/adapters/llm.py`; reuse `LLMClient` strict structured output first for `QueryIntent` and later for `QueryDraft`. Permit one corrected draft only after stable SQL validation feedback; ambiguity clarification is a Conversation turn, not SQL repair. | | |
| TASK-006 | Complete `backend/src/query/domain.py` validation for table/column IDs, forbidden statements/schemas/functions, named parameter parity, FK-supported joins and preservation of explicit user Top-N semantics. Convert a passing draft to `ValidatedQuery`; make direct construction unavailable outside the module. | | |
| TASK-007 | Add `backend/tests/unit/query/test_validation.py` with one table-driven test covering a valid aggregate without forced LIMIT, explicit Top-N preservation, multi-statement injection, DML, system schema, unknown object, raw predicate literal, cross join, dangerous function and parameter mismatch. | | |
| TASK-008 | Add `backend/tests/unit/query/test_planner.py` proving first-pass success, one repair after stable feedback, second invalid draft fail-closed, and no repair on infrastructure exceptions. | | |

### Implementation Phase 3

- **GOAL-003**: Add the read-only DW adapter and the deep application orchestration.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | Add `QuerySettings` in `backend/src/settings.py` and `backend/conf/app_config.yaml` with `read_url`, `timeout_seconds=10`, `fetch_batch_rows=500` and `max_batch_bytes=1048576`; validate `mysql+asyncmy` and require the configured database to equal `data_sync.dw_database`. | | |
| TASK-010 | Add `backend/src/query/adapters/mysql.py` with a separate async engine. Execute `SET TRANSACTION READ ONLY`, `EXPLAIN` and the validated parameterized query; stream fetchmany batches under timeout and per-batch row/byte budgets until the full result is consumed or cancelled; never expose commit or arbitrary statement methods. | | |
| TASK-011 | Add `backend/src/query/application/service.py`. Implement the single flow: parse DDL, start/replay Conversation turn, parse and ground QueryIntent, complete one clarification turn when required, retrieve QueryContext, generate, validate, EXPLAIN, repair once when eligible, check readiness from AST targets, execute in batches, complete the result turn and emit redacted audit fields. | | |
| TASK-012 | Add `backend/tests/unit/query/test_service.py` through the public application interface. Cover successful automatic execution, metric then time clarification across turns, not-ready fixed text, one SQL repair, timeout without repair, multi-batch continuation without total truncation and turn replay without duplicate execution. | | |
| TASK-013 | Add `backend/tests/integration/query/test_mysql_executor.py` using local MySQL. Prove SELECT succeeds, DML and non-DW access are denied by database grants, timeout is bounded, `EXPLAIN` does not return rows, explicit Top-N is preserved and detail results continue across deterministic batches. | | |

### Implementation Phase 4

- **GOAL-004**: Expose and compose the query flow without changing the existing Chat contract.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Add `backend/src/query/adapters/http.py` with `POST /api/v1/conversations/{conversation_uid}/query-turns`; return NDJSON `StreamingResponse` events for clarification, metadata, row batches and completion, plus the existing stable `DataAgentError` mapping before response start and a fixed safe stream error after start. | | |
| TASK-015 | Update `backend/src/application.py` to compose the existing Meta runtime for the API process, Query planner, Answer Readiness, dedicated DW executor and `QueryApplication`; close the query engine during lifespan shutdown. | | |
| TASK-016 | Add API tests under `backend/tests/unit/query/test_api.py` for success, validation failure, not-ready fixed text, timeout/error mapping, unknown request fields and idempotent replay. | | |
| TASK-017 | Run the full validation set, verify documentation and OpenAPI contracts, and keep the task in planning until the user separately approves implementation activation. | | |

## 3. Alternatives

- **ALT-001**: Add SQL tool calling directly to `ChatService._generate`. Rejected because the current free-text interface would expose unvalidated model output and spread recall, repair and execution rules through Chat.
- **ALT-002**: Run three parallel searches for fields, metrics and values. Rejected because table/column/metric already share one index and value search requires candidate column IDs.
- **ALT-003**: Add source/schema fields to every Meta projection now. Deferred because binding MVP to current DDL provides a deterministic allowlist without an index rebuild.
- **ALT-004**: Ask users to confirm SQL before execution. Rejected by the explicit product decision; security is enforced by deterministic and database-level gates instead.

## 4. Dependencies

- **DEP-001**: Existing `ddl_metadata.parsing.parse_ddl` and physical schema IDs/FK relationships.
- **DEP-002**: Existing `MetadataSearchService` and the Qdrant/Elasticsearch/TEI clients it uses.
- **DEP-003**: Existing `AnswerReadinessService` fixed readiness contract.
- **DEP-004**: Existing Conversation turn lifecycle and long-term context.
- **DEP-005**: Existing `LLMClient`, SQLGlot and async SQLAlchemy dependencies.
- **DEP-006**: A provisioned DW database account with `SELECT` only; implementation cannot claim automatic execution safety until this external grant is verified.

## 5. Files

- **FILE-001**: `backend/src/query/domain.py` — query contracts and deterministic validation.
- **FILE-002**: `backend/src/query/application/contracts.py` — application ports and public request/response models.
- **FILE-003**: `backend/src/query/application/service.py` — deep query orchestration.
- **FILE-004**: `backend/src/query/adapters/llm.py` — strict generation and repair adapter.
- **FILE-005**: `backend/src/query/adapters/mysql.py` — read-only EXPLAIN/execution adapter.
- **FILE-006**: `backend/src/query/adapters/http.py` — query-turn HTTP adapter.
- **FILE-007**: `backend/src/query/adapters/metadata.py` — current-DDL-scoped Meta retrieval adapter.
- **FILE-008**: `backend/src/settings.py` and `backend/conf/app_config.yaml` — dedicated query connection and budgets.
- **FILE-009**: `backend/src/application.py` — composition and lifecycle.
- **FILE-010**: `backend/tests/unit/query/` — interface, policy, planner and HTTP tests.
- **FILE-011**: `backend/tests/integration/query/test_mysql_executor.py` — live read-only database proof.

## 6. Testing

- **TEST-001**: `cd backend && uv run ruff check src tests`.
- **TEST-002**: `cd backend && uv run pyright`.
- **TEST-003**: `cd backend && uv run pytest tests/unit/query tests/unit/answer_readiness tests/unit/chat/test_chat_service.py -q`.
- **TEST-004**: `cd backend && uv run pytest tests/integration/query/test_mysql_executor.py -q` with a real SELECT-only DW user.
- **TEST-005**: `cd backend && uv run pytest -m 'not integration' -q`.
- **TEST-006**: `cd backend && uv run python -m compileall -q src tests`.
- **TEST-007**: `git diff --check` and `python ./.trellis/scripts/task.py validate 08-05-design-query-sql-flow`.

## 7. Risks & Assumptions

- **RISK-001**: Current metric definitions are natural-language descriptions, not executable formulas. Ambiguous metric semantics must clarify rather than be treated as deterministically validated.
- **RISK-002**: Meta semantic search has no completeness flag. Empty or unavailable projections must fail closed; the planner must not guess object names.
- **RISK-003**: Current unified DW has no source column. Results are all-source even though the metadata context is bound to one source DDL.
- **RISK-004**: `EXPLAIN` is not a complete cost guarantee. Fixed timeout, concurrency and per-batch transport budgets remain mandatory until production evidence justifies a cost threshold; a result `LIMIT` is not a scan-cost control.
- **ASSUMPTION-001**: Query requests continue to carry the current MySQL DDL and source, matching the existing Workbench/Chat context.
- **ASSUMPTION-002**: Deployment can provision and verify a dedicated SELECT-only DW account before automatic execution is enabled.

## 8. Related Specifications / Further Reading

- `.trellis/tasks/08-05-design-query-sql-flow/prd.md`
- `.trellis/tasks/08-05-design-query-sql-flow/design.md`
- `CONTEXT.md`
- `.trellis/spec/backend/index.md`
- `.trellis/spec/backend/directory-structure.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/external-service-integrations.md`
- `docs/agent-knowledge.html`
