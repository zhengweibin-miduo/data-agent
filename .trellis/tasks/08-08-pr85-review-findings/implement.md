---
goal: Close all unresolved PR 85 query correctness and execution ownership findings
version: 1.0
date_created: 2026-08-08
last_updated: 2026-08-08
owner: zwb
status: 'In Progress'
tags: [bug, query, conversation, mysql, concurrency, tdd]
---

# Introduction

![Status: In Progress](https://img.shields.io/badge/status-In_Progress-blue)

This plan implements the approved PR #85 follow-up contracts from the task PRD
through vertical TDD slices. It does not authorize task activation, commit,
push, GitHub replies, or thread resolution.

## 1. Requirements & Constraints

- **REQ-001**: Add required Query Supplemental Context with a validated IANA user time zone and include the exact zone key in Query turn idempotency.
- **REQ-002**: Resolve supported natural ranges into trusted half-open boundaries; recent N days are N user-local calendar days including today.
- **REQ-003**: Load the pending Query clarification chain from authoritative messages independently of the ordinary Conversation context window.
- **REQ-004**: Fence every Conversation turn claim with a new token on first claim and reclaim; renew, complete and abandon must compare both turn UID and token.
- **REQ-005**: Run every decisive readiness, authority and EXPLAIN operation under the corresponding generation READ set.
- **REQ-006**: Move Locking Service owners to a dedicated bounded manager and inject it into all READ/WRITE adapters and process lifecycles.
- **SEC-001**: LLM output must not supply trusted time zones, derived date boundaries, raw SQL literals or claim tokens.
- **SEC-002**: Claim tokens must remain absent from logs, audit identity and user-visible Query events.
- **CON-001**: Preserve SELECT-only Query credentials, no total-result LIMIT, NDJSON batching and all-sources result scope.
- **CON-002**: Update only the initial V1 bootstrap schema; do not add migrations, old-row backfills, compatibility shims or historical cleanup.
- **CON-003**: Do not hold generation locks across LLM draft or repair calls.
- **GUD-001**: Follow `.trellis/spec/backend/query-guidelines.md`, `conversation-memory.md`, `database-guidelines.md`, `error-handling.md` and `quality-guidelines.md`.
- **PAT-001**: Test observable behavior at the Query HTTP/stream, Conversation application/store and GenerationLockManager interfaces; do not test private methods or internal collaborator call order.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Establish Query Supplemental Context and Trusted Time Range as one end-to-end vertical slice.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add failing HTTP/Application tests in `backend/tests/unit/query/test_api.py` and `test_service.py` for required `supplemental_context.user_timezone`, invalid IANA zones and time-zone-sensitive idempotency. Then add `SupplementalQueryContext` to `backend/src/query/application/contracts.py`, `QueryTurnRequest` in `backend/src/query/adapters/http.py`, and the Query semantic fingerprint in `backend/src/query/application/service.py`. | X | 2026-08-08 |
| TASK-002 | Add failing literal-boundary tests in `backend/tests/unit/query/test_validation.py` for `YYYY年`, `YYYY年M月`, 今年, 去年, 本月, 上月, recent N calendar days, leap years, month/year rollover and a DST zone. Implement `TrustedTimeRange` plus deterministic resolution in `backend/src/query/domain.py` using an injected UTC instant and the validated IANA zone. | X | 2026-08-08 |
| TASK-003 | Add failing planner/validator tests proving both `>= start` and `< end` are mandatory for DATE, DATETIME and TIMESTAMP columns. Update `QueryPlannerPort`, `QueryLLMAdapter` prompts, `validate_query()` and `QueryApplication` to carry the trusted range separately from exact-evidence `QueryIntent`; reject duplicate/extra/missing temporal predicates. | X | 2026-08-08 |

### Implementation Phase 2

- **GOAL-002**: Fence Conversation ownership and expose a durable pending-chain interface.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Add failing Conversation repository/application tests in `backend/tests/unit/conversation/test_turn_lease.py`, `test_application.py` and `backend/tests/integration/persistence/test_conversation_repository.py` showing that a suspended old owner cannot renew, complete or abandon after reclaim. Add `active_turn_claim_token` to `backend/src/conversation/mysql_tables.py` and `docs/docker/mysql/data_agent.sql`; generate a new token for each claim/reclaim and compare it in all owner mutations. | X | 2026-08-08 |
| TASK-005 | Propagate the opaque claim token through `conversation/models.py`, `conversation/application/contracts.py`, `conversation/application/service.py`, `conversation/adapters/mysql/store.py`, `conversation/api.py`, `query/application/contracts.py`, `query/application/service.py` and `chat/service.py`. Update public two-step Conversation start/complete tests and ensure Query/Chat never emit or log the token. | X | 2026-08-08 |
| TASK-006 | Add failing store/application and Query stream tests for a clarification chain exceeding 20 messages and 32,768 characters, plus an independent-budget overflow. Implement a paged authoritative message scan in `conversation/repository.py`, expose it through Conversation contracts/adapters, add `query.clarification_chain_message_limit=100` and `query.clarification_chain_max_chars=262144` in `settings.py` and `conf/app_config.yaml`, and make `QueryApplication.stream()` use only this interface for intent context/evidence. | X | 2026-08-08 |

### Implementation Phase 3

- **GOAL-003**: Isolate generation ownership and coordinate every decisive EXPLAIN.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | Add failing unit and live MySQL tests for a dedicated bounded generation pool, READ sharing, WRITE exclusion, pool checkout timeout, cancellation, release failure and close. Implement `backend/src/infrastructure/generation_locks.py` with `pool_size=16`, `max_overflow=0`, configurable one-second checkout timeout, capability probe, atomic multi-target READ/WRITE acquisition and stable error translation; leave ordinary advisory locks in `infrastructure/mysql.py`. | X | 2026-08-08 |
| TASK-008 | Inject the generation manager into `query/adapters/readiness.py`, `ddl_metadata/adapters/mysql/accepted_snapshot.py` and `data_sync/adapters/mysql.py`; create/probe/close it in `application.py`, `ddl_metadata/worker/lifecycle.py` and `data_sync/worker.py`. Add `mysql.generation_lock_pool_size` and `mysql.generation_lock_pool_timeout_seconds` settings/configuration and update all affected composition tests. | X | 2026-08-08 |
| TASK-009 | Add a failing Query application/MySQL concurrency regression where initial readiness passes and a WRITE owner begins schema synchronization before EXPLAIN. Refactor planning so each statically valid draft acquires its own target READ set for authority, readiness and EXPLAIN, releases before repair, and reacquires for a changed repaired target; keep the final coordinated EXPLAIN and streamed SELECT recheck. | X | 2026-08-08 |

### Implementation Phase 4

- **GOAL-004**: Complete cross-layer contracts, regression verification and review evidence.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Update `CONTEXT.md`, `.trellis/spec/backend/query-guidelines.md`, `conversation-memory.md`, `database-guidelines.md`, `error-handling.md` and `quality-guidelines.md` so request context, time semantics, Turn Claim CAS, clarification-chain budgets and dedicated generation lifecycle match executable behavior. | X | 2026-08-08 |
| TASK-011 | Run focused Query, Conversation and infrastructure tests after each slice; then run `uv run pytest -m "not integration" -q`, relevant live MySQL integration modules, `uv run ruff check src tests`, `uv run pyright src tests`, `python -m compileall -q src tests`, configuration loading, Compose/CI static checks, build checks and `git diff --check`. Record unavailable external dependencies without claiming success. | X | 2026-08-08 |
| TASK-012 | Re-read PR #85 with thread-aware GraphQL, map each of the five thread IDs to code/test evidence and prepare proposed replies. Do not reply or resolve without separate explicit GitHub write authorization. | X | 2026-08-08 |

## 3. Alternatives

- **ALT-001**: Use a server-global time zone. Rejected because it violates explicit Supplemental Query Context and silently changes user-local calendar boundaries.
- **ALT-002**: Store model-derived concrete dates in Query Intent. Rejected because Query Intent is exact user evidence and model dates cannot become trusted input.
- **ALT-003**: Keep reconstructing clarification from `ConversationContext.messages`. Rejected because its production limits are intentionally smaller than a durable pending chain.
- **ALT-004**: Use only `turn_uid` plus lease time for ownership. Rejected because the same UID can be reclaimed and a paused old execution is not fenced.
- **ALT-005**: Retain Locking Service connections in `MySQLDatabase`. Rejected because long streams can consume the business transaction pool.

## 4. Dependencies

- **DEP-001**: Python standard-library `zoneinfo` and the host IANA time-zone database must resolve submitted zone keys.
- **DEP-002**: MySQL 8.4 Locking Service functions installed by the existing V1 bootstrap and startup capability probe.
- **DEP-003**: Existing PR #85 generation lock names, accepted relationship authority and SELECT-only Query executor contracts.

## 5. Files

- **FILE-001**: Query contracts and behavior: `backend/src/query/domain.py`, `application/contracts.py`, `application/service.py`, `adapters/http.py`, `adapters/llm.py`, `adapters/readiness.py`.
- **FILE-002**: Conversation contracts and persistence: `backend/src/conversation/models.py`, `application/contracts.py`, `application/service.py`, `adapters/mysql/store.py`, `repository.py`, `mysql_tables.py`, `api.py`, and `backend/src/chat/service.py`.
- **FILE-003**: Generation infrastructure and composition: `backend/src/infrastructure/generation_locks.py`, `infrastructure/mysql.py`, `application.py`, `ddl_metadata/adapters/mysql/accepted_snapshot.py`, `ddl_metadata/worker/lifecycle.py`, `data_sync/adapters/mysql.py`, and `data_sync/worker.py`.
- **FILE-004**: Configuration/bootstrap: `backend/src/settings.py`, `backend/conf/app_config.yaml`, `docs/docker/mysql/data_agent.sql`, and configuration/Compose/CI checks that enumerate required keys.
- **FILE-005**: Unit and integration tests under `backend/tests/unit/query`, `backend/tests/unit/conversation`, `backend/tests/unit/infrastructure`, `backend/tests/integration/query`, `backend/tests/integration/persistence`, and `backend/tests/integration/infrastructure`.
- **FILE-006**: Domain/spec documentation: `CONTEXT.md` and the backend Query, Conversation, Database, Error Handling and Quality guidelines.

## 6. Testing

- **TEST-001**: Literal fixed-clock time-range examples cover date types, leap/calendar rollovers, DST and unsupported/ambiguous expressions.
- **TEST-002**: Query HTTP/stream tests cover supplemental context validation, idempotency, two-round time-column clarification and pending-chain budget failures.
- **TEST-003**: Conversation concurrency tests prove old claim generations cannot renew, complete or abandon a reclaimed turn across both in-memory seam tests and real MySQL transactions.
- **TEST-004**: Generation manager tests prove independent pool capacity, atomic READ/WRITE semantics, stable checkout/contention errors, cancellation and lifecycle cleanup.
- **TEST-005**: Query/MySQL concurrency proves no decisive EXPLAIN races schema DDL and repaired drafts reacquire the correct target set.
- **TEST-006**: Full repository quality gates prove no regressions in Chat, Query, accepted snapshot, Data Sync or application startup/shutdown.

## 7. Risks & Assumptions

- **RISK-001**: IANA data can differ between hosts. Tests must use stable zones/examples and fail request validation when a submitted zone is unavailable.
- **RISK-002**: DATETIME has no absolute zone. The approved design interprets it as user-local wall time; TIMESTAMP alone is converted to UTC.
- **RISK-003**: Public Conversation completion becomes token-bearing. Every caller and test must migrate atomically within this unmerged V1 branch.
- **RISK-004**: A dedicated pool bounds damage but can reject excess concurrent streams. The rejection must be stable and retryable rather than an unmapped SQLAlchemy timeout.
- **ASSUMPTION-001**: PR #85 remains unmerged and no deployed historical V1 database requires an upgrade path.
- **ASSUMPTION-002**: All relevant API/DDL/Data Sync processes can restart together after the generation manager change.

## 8. Related Specifications / Further Reading

- `.trellis/tasks/08-08-pr85-review-findings/prd.md`
- `.trellis/tasks/08-08-pr85-review-findings/design.md`
- `.trellis/tasks/08-08-pr85-review-findings/research/evidence-map.md`
- `CONTEXT.md`
- `.trellis/spec/backend/query-guidelines.md`
- `.trellis/spec/backend/conversation-memory.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/error-handling.md`
- `.trellis/spec/backend/quality-guidelines.md`
