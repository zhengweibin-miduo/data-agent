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

Starting a turn atomically inserts the user message and sets
`active_turn_uid`. It returns the persisted message plus a bounded context:
the current summary, recent messages after its cursor, and relevant active
`user.*` rows for the same `user_id`. Completing a turn atomically inserts
the assistant message, inserts one extraction outbox row, releases the active
turn, and only then reports success. Replaying the same `turn_uid` and content
returns the existing result.

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
  content_version: v2
  projection_version: v2
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
- Bad: `OK`, `yes`, or an unrelated later user statement is treated as evidence
  for an assistant claim.
- Bad: filtering `user_id` only after ES/Qdrant search or only before returning
  the response.
- Bad: retaining conversation history in a 24-hour Redis checkpoint or deleting
  shared memories when one conversation is deleted.

### 6. Tests Required

- Contract tests must assert that text-only requests reject empty, oversized,
  attachment, multimodal, unsupported-role, and unknown-field payloads.
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
