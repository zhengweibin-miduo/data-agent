# PR #85 unresolved review evidence map

## Verified baseline

- Pull request: `zhengweibin-miduo/data-agent#85`.
- PR base: `master`.
- Task start point: `ccb9c02d1b8ce2340f9c9270f8b23e29941607ed`.
- Thread-aware GraphQL read on 2026-08-08 returned 227 total review threads and
  5 unresolved threads. Four are current and one is outdated.

## Finding map

| Thread | Current evidence | Root contract gap |
| --- | --- | --- |
| `PRRT_kwDOTXnY3c6XW7Rp` | `backend/src/query/domain.py`; `QueryIntent.time_filter` is one `FilterIntent` | A natural year or recent-day range cannot express two trusted boundaries after time-column clarification. |
| `PRRT_kwDOTXnY3c6XctZA` | `backend/src/query/domain.py`; `_normalized_filter_values()` only normalizes numeric values and explicit localized dates | Relative range words cannot become executable dates without violating verbatim evidence. |
| `PRRT_kwDOTXnY3c6XXNaf` | `backend/src/query/application/service.py`; `evidence_chain` is rebuilt from `StartTurnResponse.context.messages` | The production 20-message/32,768-character Conversation window can discard the original question. |
| `PRRT_kwDOTXnY3c6Xcmf9` | `backend/src/conversation/repository.py`; claim, renew, complete and abandon match `active_turn_uid` only | Reclaiming the same `turn_uid` does not fence a suspended earlier execution generation. |
| `PRRT_kwDOTXnY3c6XUMt9` | `backend/src/query/application/service.py`; `_plan()` performs a readiness/EXPLAIN attempt before the final `QueryReadinessPort.hold()` region | A decisive EXPLAIN can race accepted snapshot/schema DDL. This thread is outdated but intentionally unresolved. |

## Existing contracts to preserve

- `CONTEXT.md` defines Query Intent as exact user evidence and Validated Query
  as the output of deterministic gates.
- `.trellis/spec/backend/query-guidelines.md` requires final readiness,
  relationship authority, EXPLAIN and streamed SELECT to hold generation READ
  locks and limits intent evidence to an explicitly pending clarification chain.
- `.trellis/spec/backend/conversation-memory.md` makes MySQL authoritative for
  messages and turn ownership; clarification messages do not advance the memory
  summary cursor.
- `.trellis/spec/backend/database-guidelines.md` requires source-query,
  replication and DW sessions to use UTC so MySQL `TIMESTAMP` has one absolute
  interpretation.
- `08-07-query-generation-read-coordination` already established MySQL 8.4
  Locking Service READ/WRITE semantics, atomic multi-target acquisition and
  startup capability probing. This task must deepen that implementation with a
  dedicated bounded owner pool, not introduce another lock protocol.

## Resolved product decisions

- User time zone is supplied explicitly by Query Supplemental Context as an
  IANA zone. It is not inferred from the server, database, profile or DDL.
- `recent N days` means N user-local calendar days including today. Its trusted
  interval is `[local today - (N - 1) days at 00:00, local tomorrow at 00:00)`.
- The project remains initial V1. Schema changes update bootstrap definitions
  only; no upgrade migration, historical backfill or compatibility shim is in
  scope.

## Public seams selected for TDD review

1. `POST /api/v1/conversations/{conversation_uid}/query-turns` for request
   validation and observable Query events.
2. `QueryApplication.stream(QueryRequest)` for clarification reconstruction,
   time normalization, coordinated planning and streamed execution.
3. Conversation application/store interfaces for Turn Claim acquire, renew,
   complete, abandon and durable pending-chain reads.
4. The generation lock manager interface, verified with real independent MySQL
   connections for READ/WRITE coordination and bounded pool behavior.
