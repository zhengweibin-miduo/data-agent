# Natural-Language Query Guidelines

## Scenario: Bounded Read-Only DW Query

### 1. Scope / Trigger

- Trigger: add or change natural-language business-row queries, Meta grounding,
  query SQL validation, DW execution, or NDJSON result streaming.
- Keep this flow in the `query` bounded context. Existing `chat-turns` remains
  DDL collaboration and must not execute business-row SQL.

### 2. Signatures

- HTTP: `POST /api/v1/conversations/{conversation_uid}/query-turns`.
- Application: `QueryApplication.stream(QueryRequest) -> AsyncIterator[QueryEvent]`.
- Executor: `explain(ValidatedQuery) -> None` and
  `execute(ValidatedQuery) -> AsyncIterator[QueryBatch]`.
- Configuration: `query.read_url`, `query.timeout_seconds`,
  `query.fetch_batch_rows`, and `query.max_batch_bytes`.

### 3. Contracts

- `QueryIntent` contains exact user-message quotes only. A time range and its
  physical time-column quote are separate evidence.
- The Meta adapter converts Meta Projection DTOs into Query-owned candidates;
  Query domain/application code must not import concrete Meta Projection models.
- `QueryDraft` is untrusted model output. Only `validate_query` can create a
  `ValidatedQuery`, and the application receives `dw_database` by injection.
- Draft and repair prompts receive that same configured `dw_database`; prompts
  must not assume the default `dw` schema name.
- Query turn idempotency covers the question, source, and parsed schema
  fingerprint. Reusing a `turn_uid` with different query semantics is a
  conflict, not a completed-result replay.
- Persist the Query terminal event kind with the assistant turn so idempotent
  replay preserves `clarification` instead of converting it to `complete`.
- Results use NDJSON events: one `metadata`, zero or more `rows`, then
  `complete`; post-start failures emit one safe `stream_error`.
- The `metadata` event declares `result_scope="all_sources"`; request
  `ddl_context.source` scopes metadata grounding only and never filters DW rows.
- The dedicated MySQL URL must select the configured DW database and use a
  database user distinct from the writable application user. Provision that
  user with `SELECT` on `dw.*` only.

### 4. Validation & Error Matrix

- Missing or ambiguous grounding -> one highest-impact `clarification`; do not
  generate SQL.
- Comments, multiple statements, DML/DDL, locks, user variables, file output,
  dangerous functions, system/non-DW schemas, unknown objects, `SELECT *`,
  unsupported joins, raw predicate literals, or parameter mismatch -> stable
  validation issue; permit at most one model repair.
- The AST must exactly preserve the intent's result shape, filter and time
  predicates, time grain, sort objects and directions, Top-N, and absence of
  pagination offsets. Every `JOIN ON` condition must be an allowlisted FK edge;
  one valid edge never authorizes additional boolean conditions.
- Until separately evidenced, predicates may only be joined with `AND` and
  `DISTINCT` is forbidden. Aggregate validation is limited to the top-level
  projection and binds both the function and its operand to the selected
  measure; grouping and time buckets must bind exactly to their selected
  dimension or time column.
- Chat and Query use entrypoint-specific semantic fingerprints and both obey
  execution ownership. Query intent reconstruction may consume bounded
  role-labelled clarification history, while final evidence quotes must still
  occur verbatim in user messages.
- Reject negated predicates unless the trusted intent explicitly models the
  negation. Aggregate functions require an exact user-evidenced operation, and
  quarter buckets must retain a year coordinate.
- Every filter operator requires verbatim user evidence. `WHERE` and `HAVING`
  fail closed unless their complete trees consist only of supported atoms
  joined by `AND`; detail projections exactly match bound result fields.
- Unsupported negative operator phrases fail closed before planning. Scalar and
  grouped result projections must be the exact trusted aggregate, dimension,
  and time-bucket expressions; merely containing a trusted subtree is not
  sufficient. Scoped Meta retrieval must not apply the global display Top-K
  when its result is used to prove that a binding is unique.
- Query execution emits a structured `started` audit event before invoking the
  read-only executor and a structured terminal event independently of
  Conversation completion. Audit identity includes user, conversation, turn,
  SQL hash, table IDs, duration, row count, and outcome, but no parameters or
  business rows.
- `EXPLAIN` semantic rejection -> one repair; timeout, connection, permission,
  readiness, or execution failures -> no model repair.
- Any target table not ready -> exactly `数据准备中，请稍后重试`.
- Execution timeout -> `query_timeout`; an oversized single row ->
  `query_row_too_large`; never add a total-result `LIMIT` as a safety control.
- The async driver fetches configured row batches; byte-budget splitting stays
  incremental in process so streaming does not degrade to one cursor await per row.
- Derived-table outputs may inherit a physical column identity only through a
  direct column projection; formulas and set operations fail closed without an
  explicit lineage contract. Natural-language metrics with multiple related
  columns require clarification instead of treating those columns as a formula.
- The final readiness check, relationship-authority revalidation, `EXPLAIN`,
  and streamed read hold one atomic ordered set of Locking Service READ locks.
  Accepted snapshot publication, schema synchronization, and generation reset
  use the matching WRITE locks, so queries can share a stable generation while
  no generation can be replaced between validation and execution.
- Locking Service timeout or deadlock maps to retryable
  `generation_lock_unavailable` at `query_readiness` with HTTP 409 before the
  first event, or the same safe code in `stream_error` after response start.
- A generation READ-lock release failure invalidates the owner connection. If
  the Query body already completed, log the operational warning without
  appending `stream_error` after the terminal `complete` event; an active Query
  exception remains authoritative.
- Evidence normalization must reject ambiguous operator or grain phrases and
  must validate a time filter's complete clause quote just like ordinary filters.
- Derived output aliases cannot inherit an unrelated physical-column identity;
  numeric-only aggregates must validate the authoritative physical column type,
  and detail ranking projections must exactly cover their bound result fields.
- Scoped Meta candidates cannot prove uniqueness while any recalled semantic
  projection is pending convergence. Conversation context-read failures after a
  turn claim must release execution ownership immediately.
- JOIN authorization requires the request schema fingerprint to match the
  authoritative accepted physical-schema snapshot; request DDL alone never
  authorizes relationships. Intent evidence is limited to the current request
  plus an explicitly pending clarification chain, not arbitrary completed turns.
- Explicit distinct semantics fail closed until modeled, and every time bucket
  operand must be an authoritative temporal physical type.
- Stream backpressure does not consume the database I/O budget; accepted
  relationship fingerprints are revalidated while generation locks are held.
- Filter normalization follows the authoritative column type so textual IDs
  retain leading zeroes, and public projection aliases cannot relabel direct
  physical fields as another business field.
- A live Query owner renews its Conversation lease independently of response
  consumption, so a healthy stream cannot be reclaimed after the lease age.
- Role-labelled clarification context and verbatim user evidence are separate
  intent inputs. Multi-round clarification keeps the original question and all
  user answers until a non-clarification terminal result closes the chain;
  clarification messages do not advance the memory-summary cursor.
- Explicit filters and non-temporal grouping dimensions must be covered item by
  item by the trusted intent; a non-empty partial slot list is not sufficient.
- Accepted physical-schema authority is scoped by the submitted table set, so
  independently accepted table snapshots for one source coexist; detail result
  fields remain item-complete even when the same request also has filters.

### 5. Good/Base/Bad Cases

- Good: bind exact phrases to current-DDL Meta IDs, validate a parameterized FK
  join, run `EXPLAIN`, check AST target tables, then stream every result batch.
- Base: a completed `turn_uid` replays its persisted completion message and
  does not execute the database again.
- Bad: use model confidence to choose a metric, treat top-N value projection as
  a complete enum, execute `QueryDraft`, or reuse the writable MySQL session.

### 6. Tests Required

- Unit-test exact quote evidence, empty/ambiguous intent, time-column
  clarification, cross-source rejection, Meta-before-value ordering, and
  incomplete value projection semantics.
- Unit-test CTEs, aliases, named placeholders, Top-N preservation, allowlisted
  FK joins, all forbidden SQL classes, and event-loop responsiveness.
- Unit-test empty-result columns, generator cleanup, safe stream errors,
  multi-batch continuation, and idempotent replay without duplicate execution.
- Live MySQL integration must prove SELECT succeeds while DW writes and
  non-DW reads fail under the configured query user. Report the service as
  unavailable when the local MySQL endpoint is down.

### 7. Wrong vs Correct

#### Wrong

```python
draft = await planner.draft(context, intent)
async for row in writable_session.stream(text(draft.sql)):
    yield row
```

#### Correct

```python
for attempt in range(2):
    result = await validate_query(draft, context, intent, dw_database=dw_database)
    if result.validated is not None:
        validated = result.validated
        break
    if attempt == 1:
        raise DataAgentError("query_unsafe", "query_validation", "查询未通过安全门禁")
    draft = await planner.repair(context, intent, draft, result.issues)
await readonly_executor.explain(validated)
async for batch in readonly_executor.execute(validated):
    yield batch
```
