# Design: close PR #85 query correctness and ownership gaps

## 1. Design goals and invariants

This task deepens three existing modules without changing the Query endpoint's
SELECT-only or NDJSON result contract:

1. Query owns deterministic temporal interpretation. LLM output may identify
   exact user quotes but may not invent a time zone, operator or concrete date.
2. Conversation owns durable clarification history and Turn Claim fencing.
   Query and Chat consume those interfaces instead of reconstructing ownership.
3. Infrastructure owns generation READ/WRITE connections behind a dedicated,
   bounded manager. Business transactions and streamed query execution never
   lend their pools to long-lived lock owners.
4. Every readiness, authority check and EXPLAIN that decides repair or a request
   terminal state runs while the matching generation READ set is held.

## 2. Domain model

### Supplemental Query Context

Add a Query-owned request value object with required
`user_timezone: str`. Validation constructs `zoneinfo.ZoneInfo` and rejects an
unknown IANA key at the HTTP/Application contract. The object is separate from
`DDLJobRequest`: DDL context owns physical source/schema authority, while
Supplemental Query Context owns interpretation facts supplied for this request.

The exact validated zone key participates in the Query semantic fingerprint.
Reusing a `turn_uid` with another zone is therefore an idempotency conflict.

### Trusted Time Range

`QueryIntent` continues to contain only exact user quotes. A pure Query domain
function resolves a supported natural range only after evidence validation and
time-column binding. Its small interface accepts the intent, authoritative
physical column type, validated IANA zone and an injected UTC `datetime`, and
returns either no range or one `TrustedTimeRange` containing the source quote,
bound column identity, inclusive lower bound and exclusive upper bound.

Supported grammar is intentionally closed:

- `YYYY年`;
- `YYYY年M月`;
- `今年`, `去年`, `本月`, `上月`;
- `最近 N 天`, where `1 <= N <= 3660` and the window contains today in the
  supplied user zone.

Boundary encoding depends on the authoritative MySQL type:

- `DATE`: bind local ISO dates.
- `DATETIME`: bind user-local wall-clock values because MySQL does not attach
  an offset to this type.
- `TIMESTAMP`: convert local boundaries to UTC and bind UTC wall-clock values;
  Query/MySQL sessions already run in UTC.

The planner receives `TrustedTimeRange` separately from `QueryIntent` and is
instructed to emit `column >= :start AND column < :end`. `validate_query()`
requires the exact bound column, both operators, both parameter names and both
trusted values. It rejects an omitted boundary, extra temporal predicate,
reversed range, raw literal or mismatched type. Existing explicit one-sided
`time_filter` behavior remains separate and cannot be combined with a natural
range for the same quote.

No ADR is required: the glossary terms are new, but the implementation choices
remain reversible inside the unmerged V1 Query contract.

## 3. Conversation interfaces

### Turn Claim fencing

Add nullable `active_turn_claim_token VARCHAR(32)` to the initial
`agent_conversation` bootstrap/table definition. Every first claim and every
expired/abandoned reclaim generates a new `uuid4().hex` token in the same
transaction that writes `active_turn_uid`.

The Conversation interface returns the token only when
`execution_owner=True`. First completion, renew and abandon require both
`turn_uid` and `claim_token` and use compare-and-set predicates. Reclaim replaces
the token, so an earlier execution generation cannot mutate the new claim.
Successful completion clears UID, token and abandoned coordinate atomically.
Abandon clears the token and writes the existing reclaim sentinel; an old
heartbeat then fails immediately. Completed-message idempotent replay remains
read-only and may return the existing message even though its old token is no
longer active.

The public two-step Conversation endpoint returns the opaque claim token from
start and requires it on assistant completion. Server-owned Chat and Query keep
the token internal but pass it through heartbeat, completion and finally
cleanup. The token is a capability coordinate and must not appear in logs.

Only the V1 bootstrap schema changes. There is no ALTER script, old-row
backfill, nullable compatibility branch or migration framework.

### Durable pending clarification chain

Add one Conversation interface that reads the Query evidence chain from
authoritative messages through the current user message ID. Its MySQL adapter
walks messages backward until it reaches the previous non-clarification
assistant terminal or the beginning of the conversation, then returns the chain
in chronological order. It ignores the ordinary summary cursor and bounded
Conversation context window.

The scan has independent Query configuration:

- `query.clarification_chain_message_limit: 100`;
- `query.clarification_chain_max_chars: 262144`.

If reaching either limit prevents proving the chain boundary, the interface
fails closed with a stable safe error. The Query application does not call the
intent model, EXPLAIN or SELECT after that error. A current user message not
preceded by a Query clarification is a one-message chain. Assistant text is
role-labelled context only and never becomes user evidence.

This design reuses persisted assistant semantic fingerprints as durable chain
markers; it does not add another chain table or coordinate that could diverge
from message authority.

## 4. Generation coordination module

Extract Locking Service ownership from the global `MySQLDatabase` business pool
into a deep `GenerationLockManager` module. Its interface is limited to:

```python
await manager.initialize()
await manager.check_capability()
async with manager.read(names, timeout_seconds): ...
async with manager.write(names, timeout_seconds): ...
await manager.close()
```

The implementation owns a dedicated SQLAlchemy engine using the existing Meta
MySQL URL, UTC session setup, stable generation namespace, atomic sorted
multi-name acquisition, release/invalidation rules and error translation.
Configuration is explicit and shared by all process composition roots:

- `mysql.generation_lock_pool_size: 16`;
- `mysql.generation_lock_pool_timeout_seconds: 1`.

The manager uses `pool_size=16`, `max_overflow=0` and the configured checkout
timeout. Pool checkout timeout maps to the same stable retryable generation
resource-busy result as server lock contention. This pool is the capacity gate:
long Query streams can occupy at most 16 generation-owner connections in the
API process and cannot exhaust `MySQLDatabase` transactions or the independent
SELECT-only Query executor pool.

Query readiness, accepted snapshot publication and Data Sync schema/reset
adapters receive the manager at composition. API, DDL worker and Data Sync
worker each initialize, probe and close their process-local manager. Existing
ordinary `GET_LOCK()` advisory locks remain on `MySQLDatabase`.

## 5. Coordinated Query planning flow

Each draft attempt follows this sequence:

```text
static AST validation
  -> derive actual target tables
  -> generation READ set
     -> relationship and binding authority checks
     -> readiness check
     -> EXPLAIN
  -> release READ set
  -> return validated draft, preparing result, or one repair issue
```

The LLM repair call occurs after releasing the READ set. A repaired draft runs
the complete sequence again and may acquire a different target set. A
successful planning attempt still enters the existing final execution region,
where authority, readiness and EXPLAIN are repeated under READ locks immediately
before streamed SELECT. Therefore no decision is based on a lock-free database
preflight, and no generation replacement between planning and execution can
escape the final revalidation.

## 6. Module, interface, seam and dependency rules

- Query domain is an in-process deep module. Time parsing and boundary
  calculation remain pure and are tested directly through their domain
  interface with literal expected boundaries.
- Conversation application/store is the seam for MySQL message authority and
  Turn Claim state. Query and Chat do not import tables or repository helpers.
- `GenerationLockManager` is the infrastructure seam. Adapters depend on its
  small interface; application/domain code continues to depend only on
  `QueryReadinessPort` or context-specific application ports.
- Composition roots select concrete adapters and own resource lifecycle.
- Tests replace internal-collaborator assertions with observable outcomes at
  the selected seams; time is the only in-process boundary supplied by a fake
  clock.

## 7. TDD seams and vertical slices

The implementation uses these pre-agreed public seams:

1. Query HTTP contract: supplemental context validation and NDJSON outcome.
2. `QueryApplication.stream()`: durable clarification, trusted time range,
   repair and generation-coordinated execution behavior.
3. Conversation application/store interfaces: claim/reclaim, CAS mutations and
   pending-chain reads.
4. `GenerationLockManager`: real MySQL shared/exclusive behavior, pool capacity,
   cancellation and close.

Each slice begins with one failing behavior test, implements only that behavior,
then moves to the next slice. Tests assert returned events, persisted messages,
claim outcomes and lock entry behavior, not private method calls or collaborator
call counts.

## 8. Compatibility, rollout and rollback

- PR #85 is unmerged V1, so the Query request contract may require
  `supplemental_context` without a legacy fallback.
- All processes using generation coordination must deploy and restart together;
  mixed global-pool and dedicated-manager ownership is not supported.
- Fresh MySQL initialization creates the claim-token column. Existing volumes
  are outside scope and are not modified automatically.
- Code rollback restores the prior request/schema contract and requires a
  coordinated process restart. No business rows or migration state require
  rollback.
- GitHub replies and thread resolution occur only after full verification and
  separate explicit write authorization.

## 9. Alternatives rejected

- Global/default time zone: rejected because it silently changes user-local
  natural dates and violates the supplied-context decision.
- Rolling N-by-24-hour window: rejected because the user selected calendar days
  including today.
- Persisting derived date boundaries in Query Intent: rejected because Query
  Intent must remain exact user evidence.
- Reusing the ordinary bounded Conversation context: rejected because it cannot
  prove long pending chains.
- Matching claim ownership only by UID or lease timestamp: rejected because it
  cannot fence a suspended earlier execution generation.
- Keeping Locking Service owners on the global business pool: rejected because
  long streams can starve unrelated transactions.
