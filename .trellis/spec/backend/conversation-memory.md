# Conversation and Long-Term User Memory

## Scenario: Permanent Text Conversations with Mem0-Style Recall

### 1. Scope / Trigger

Use this contract whenever code changes permanent Agent conversations,
conversation turns, bounded context, asynchronous memory extraction, or
cross-conversation user-memory recall. MySQL is authoritative for conversations,
messages, extraction work, and memory. Redis checkpoints remain temporary DDL
workflow recovery only. The first version accepts text messages from one Agent;
attachments, multimodal payloads, authentication, and Agent registration are
out of scope.

### 2. Signatures

The public HTTP surface is:

```text
POST   /api/v1/conversations
GET    /api/v1/conversations?user_id=<id>&before=<row-id>&limit=<1..100>
GET    /api/v1/conversations/{conversation_uid}/messages?user_id=<id>&before=<row-id>&limit=<1..100>
DELETE /api/v1/conversations/{conversation_uid}?user_id=<id>

POST   /api/v1/conversations/{conversation_uid}/turns
POST   /api/v1/conversations/{conversation_uid}/turns/{turn_uid}/assistant
POST   /api/v1/conversations/{conversation_uid}/chat-turns

GET    /api/v1/users/{user_id}/memories/search?query=<text>&limit=<1..100>
GET    /api/v1/users/{user_id}/memories/{memory_uid}
GET    /api/v1/users/{user_id}/memories/{memory_uid}/history
PATCH  /api/v1/users/{user_id}/memories/{memory_uid}
DELETE /api/v1/users/{user_id}/memories/{memory_uid}
DELETE /api/v1/users/{user_id}/conversation-data
```

The authoritative MySQL surface is:

```text
data_agent.agent_conversation
  UNIQUE(uid), INDEX(user_id, updated_at, id)

data_agent.agent_message
  UNIQUE(uid), UNIQUE(conversation_id, turn_uid, role)
  INDEX(user_id, conversation_id, id)

data_agent.conversation_memory_outbox
  UNIQUE(conversation_id, turn_uid)
  INDEX(available_at, lease_expires_at, id)

data_agent.agent_memory
  category + memory_key identify a logical fact
  active_key UNIQUE enforces one ACTIVE version per logical fact
  user_id NULL for DDL memory
  user_id NOT NULL and source=data_agent_conversation for user.* categories
```

`docs/docker/mysql/data_agent.sql` defines a fresh environment.
`docs/docker/mysql/` contains bootstrap creation definitions only; it does not
contain `ALTER TABLE`, data updates, or upgrade scripts for initialized
environments. Do not introduce a second memory stack, ORM, migration framework,
or queue.

### 3. Contracts

All request models forbid unknown fields. `user_id` is 1 to 128 characters;
`turn_uid` is 1 to 64 characters; user and assistant `content` is non-empty
text bounded by `conversation.max_message_chars`.

`POST .../chat-turns` is the server-owned current-DDL orchestration boundary.
Its request contains `user_id`, `turn_uid`, `content`, and `ddl_context` with a
1-to-128-character `source`, literal `dialect=mysql`, and non-empty DDL. Its
response contains the persisted assistant `MessageRecord` and one safe
`readiness` value: `proceed`, `data_preparing`, or `intent_unresolved`.

The API process requires `DATA_AGENT_LLM_API_KEY`; browsers never receive that
key or call the OpenAI-compatible endpoint directly. The application initializes
one shared `LLMClient`, injects it into the readiness classifier and chat service,
and closes it before the other shared resources during lifespan shutdown.

Starting a turn atomically inserts the user message and sets
`active_turn_uid` plus a new opaque claim token. It returns the persisted
message, the claim token only to the execution owner, and a bounded context:
the current summary, recent messages after its cursor, and relevant active
`user.*` rows for the same `user_id`. Completing a turn atomically inserts
the assistant message, inserts one extraction outbox row, releases the active
turn by matching both ownership coordinates, and only then reports success.
Replaying the same `turn_uid` and content returns the existing result read-only.

Chat orchestration validates and parses the bounded DDL before claiming a turn,
then runs `start_turn -> readiness -> model -> complete_turn`. The prompt contains
only the fixed DDL-assistant policy, canonical current DDL, source, bounded
conversation context, and authoritative user-memory hits. It may explain or
draft a DDL clarification answer, but it cannot submit
`/metadata/ddl-jobs/{job_id}/answers`; only an explicit user confirmation may use
that job contract. A failed model or completion call abandons only its own claim
generation, so the client can safely retry the same `turn_uid`, content, source,
and DDL without allowing a stale owner to affect a later reclaim. Chat renews
its claim while readiness and model work are in flight; a confirmed renewal
loss fences the stale owner before it can persist a response.

History uses the auto-increment row ID as an exclusive `before` keyset cursor.
Rows are selected newest-first for paging and returned oldest-first for display.
Every conversation, message, outbox, memory, ES, and Qdrant operation carries
the same `user_id` boundary. Search candidates are accepted only after an
authority lookup in MySQL with that boundary.

PATCHing a user-scoped memory locks its current authority row, appends a new
user-confirmed ACTIVE version, and returns `requires_reprocess=false`
immediately. It does not acquire the DDL source mutation lease or wait for a
DDL workflow because it does not project into Meta. The shared memory service's
DDL-scoped branch instead acquires that source lease and returns
`requires_reprocess=true`: the new memory authority version is active
immediately, while applying the correction to Meta still requires a complete
DDL workflow.

The extraction worker claims only the earliest eligible turn per conversation,
commits a short lease before calling the LLM, and bounds each claim wave by LLM
concurrency. A candidate is accepted only when its exact user quote occurs in
an owned evidence message. An assistant conclusion additionally requires the
assistant quote and a later user message that repeats that conclusion.
Summary cursors only advance. Because `available_at` is written by a MySQL
default, claim eligibility and lease expiry also use MySQL `NOW()`; mixing the
application clock with the database clock can hide newly created work during
clock drift.

Required YAML keys are:

```yaml
memory:
  content_version: v1
  projection_version: v1
conversation:
  max_message_chars: 32768
  context_message_limit: 20
  context_max_chars: 32768
  summary_max_chars: 4096
  extraction_batch_size: 10
  extraction_lease_seconds: 180
```

Deleting a conversation hard-deletes its messages and pending extraction rows
but retains shared user memory. Deleting user conversation data immediately
removes conversations from recall, tombstones all eligible user memories, and
keeps DELETE desired states until ES and Qdrant acknowledge them. Only then may
the purge worker physically remove memory, links, and events.

### 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| Empty or oversized content, unknown field, attachment, multimodal payload, or unsupported role | FastAPI/Pydantic `422` |
| Unknown conversation or a conversation owned by another user | `404 conversation_not_found` |
| Unknown memory or a memory owned by another user | `404 memory_not_found` |
| A different turn is already active in the conversation | `409 conversation_busy` |
| Reused `turn_uid` has different content | `409 idempotency_conflict` |
| Assistant completion does not match the active turn | `409 stale_turn` |
| Chat DDL exceeds `api.max_ddl_bytes` | `422 ddl_too_large` before a turn is claimed |
| Chat DDL is invalid or exceeds parser table/column limits | Existing deterministic DDL validation error before a turn is claimed |
| Chat model connection, timeout, rate limit, or server call fails | `502 chat_model_failed`; `retryable` reflects the upstream error class |
| Chat model returns empty, non-text, or oversized content | `502 chat_model_invalid`, retryable |
| Readiness cannot resolve data dependencies | Persist and return `intent_unresolved` with the fixed safe user message |
| Required DW data is not ready | Persist and return `data_preparing` with `数据准备中，请稍后重试` |
| Memory update races a newer authority version | `409 stale_memory` |
| Extraction LLM call, validation, or lease finalization fails | Persisted messages remain readable; outbox retries with bounded backoff |
| ES, Qdrant, or TEI fails | MySQL remains authoritative; projection work remains retryable |
| Candidate quote or ownership evidence fails validation | Discard the candidate; never create long-term memory |

### 5. Good / Base / Bad Cases

- Good: a user says `I prefer concise answers`; the exact quote and message UID
  produce one `user.preference` fact keyed by `answer_style`, which is recalled in another conversation
  for the same user.
- Good: an assistant proposes a business rule and a later user message repeats
  the rule explicitly; both messages are referenced and the confirmed
  conclusion may become memory.
- Base: a completed turn produces no durable fact; the worker advances the
  bounded summary and removes the extraction outbox without creating memory.
- Base: summary extraction is delayed; context falls back to bounded recent raw
  messages and chat remains available.
- Good: a user asks what `orders.total` means for the current DDL; the server
  uses bounded conversation context and returns a persisted draft without
  advancing the DDL job.
- Base: readiness reports that no DW rows are required for a schema-semantic
  question; chat proceeds without reading `data_sync`.
- Bad: browser JavaScript receives the LLM key, calls the model directly, or
  treats assistant prose as a confirmed metric answer.
- Bad: a failed chat request retries with a new `turn_uid` and collides with the
  still-leased turn instead of replaying the same idempotency coordinates.
- Bad: `OK`, `yes`, or an unrelated later user statement is treated as evidence
  for an assistant claim.
- Bad: filtering `user_id` only after ES/Qdrant search or only before returning
  the response.
- Bad: retaining conversation history in a 24-hour Redis checkpoint or deleting
  shared memories when one conversation is deleted.

### 6. Tests Required

- Contract tests must assert that text-only requests reject empty, oversized,
  attachment, multimodal, unsupported-role, and unknown-field payloads.
- Chat tests must assert route shape, current-DDL prompt contents, readiness safe
  messages, same-turn replay without a second model call, retryable model error
  projection, invalid/oversized DDL rejection before `start_turn`, and assistant
  persistence failure propagation. Frontend checks must assert that failed chat
  retries reuse the same `turn_uid` and that SSE `waiting_input` is refreshed
  from the authoritative job record before answers are enabled.
- Repository integration tests must assert keyset order, tenant isolation, one
  active turn, same-content idempotency, conflicting-content rejection, and
  atomic assistant-message/outbox completion.
- Extraction tests must assert exact quote ownership, assistant-guess rejection,
  ambiguous-confirmation rejection, explicit later confirmation acceptance,
  unrelated-statement rejection, one candidate per user scope, oldest-turn
  claiming, bounded concurrency, lease compare-and-set, and monotonic summaries.
- Memory tests must assert `user_id` in UID/scope/hash authority, duplicate
  `NOOP`, versioned corrections, `SUPERSEDED` history, expiry, soft delete,
  delete outbox replay, delete-before-purge ordering, ES/Qdrant category
  filters, and MySQL post-search authority checks. Raw metric questions and
  answers remain evidence inside `ddl.metric`; they are not memory rows.
- Configuration tests must assert content/projection version `v2`. Schema tests
  must compare SQLAlchemy MySQL DDL with the fresh bootstrap contract. This
  rebuild intentionally does not migrate old memory rows; an incompatible
  environment requires exact-target MySQL reprovisioning followed by explicit
  ES/Qdrant recreation and full projection rebuild.
- The quality gate is `uv lock --check`, Ruff, Pyright, `compileall`, settings
  load, non-integration pytest, compose rendering, SQLAlchemy MySQL DDL
  compilation, and `git diff --check`. Run live dependency tests only when
  MySQL, Redis, ES, Qdrant, TEI, and the configured LLM are available.

### 7. Wrong vs Correct

#### Wrong

```python
# The index is not authoritative and this leaks across tenants.
hits = await qdrant.search(query)
return hits[:limit]
```

#### Correct

```python
candidate_uids = await qdrant.search(
    query,
    user_id=user_id,
    categories={"user.preference", "user.constraint"},
    source="data_agent_conversation",
)
return await memory_repository.get_many_active(
    candidate_uids,
    user_id=user_id,
)
```

The same tenant predicate must exist at the index query and authoritative MySQL
lookup boundaries; response-time filtering alone is not isolation.

#### Wrong

```javascript
// This leaks the model credential and bypasses conversation authority.
await fetch(modelUrl, { headers: { Authorization: apiKey }, body: userText });
```

#### Correct

```javascript
await fetch(`/api/v1/conversations/${conversationUid}/chat-turns`, {
  method: "POST",
  body: JSON.stringify({ user_id, turn_uid, content, ddl_context }),
});
```

The server owns model credentials, bounded context, readiness, idempotency, and
assistant-message persistence. The browser owns only explicit user interaction.

### Turn Claim and Query Clarification Contracts

- Each first claim and expired reclaim creates a new opaque claim token. Renew,
  complete and abandon compare both `turn_uid` and token; a suspended older
  owner cannot mutate a reclaimed generation.
- The token is an internal ownership credential. Do not log it, include it in
  audit identity, or emit it in Query/Chat streaming events. The public
  Conversation start/complete protocol may carry it only for the two-step owner.
- Pending Query clarification uses authoritative persisted messages, stopping at
  the preceding non-clarification assistant result. Its message and character
  budgets are independent of summary/context limits; overflow fails closed with
  `query_clarification_chain_too_large`.

- Failed turns keep an independent `turn_abandoned_at` lease coordinate: the
  same `turn_uid` may retry immediately, while a different turn may take over
  only after the configured finite lease expires.
