# Research: durable Agent conversations and cross-conversation memory

- Query: Locate the smallest repository-faithful extension points for permanent text-only Agent conversations, bounded chat context, and Mem0-style user memory shared across conversations.
- Scope: internal
- Date: 2026-07-20

## Findings

### Requirements held constant

- One `user_id` owns multiple conversations.
- MySQL permanently stores raw `user` and `assistant` text until explicit deletion.
- Long-term memory is shared by the same user across conversations.
- Redis/LangGraph checkpoints remain DDL task recovery state only.
- Conversation context is: current conversation summary + bounded recent messages + relevant user long-term memory + current input.
- Long-term extraction runs asynchronously from a durable MySQL outbox after the round's messages are durable.
- An assistant statement is not memory evidence unless a later user message explicitly confirms it.
- The first version has one Data Agent, loopback HTTP with no authentication, strict `user_id` filtering, no UI, attachments, multimodal roles, or per-message vector index.

### Existing composition and contracts

- `src/data_agent/application.py:36` constructs the existing `MemoryService` in the FastAPI lifespan; `src/data_agent/application.py:120` includes one aggregate DDL router. A conversation router and service can be composed at these same two points without another application or resource lifecycle.
- `src/data_agent/ddl_metadata/api/router.py:9-10` is only a DDL feature router. Conversation routes should be a sibling feature router included by `application.py`, not nested under `/metadata`.
- `src/data_agent/settings.py:119` and `conf/app_config.yaml:37` enforce `127.0.0.1`; this already satisfies the no-auth loopback boundary. The future trusted auth layer is absent.
- Shared request/response contracts use Pydantic with forbidden extra fields (`src/data_agent/settings.py:22`; `src/data_agent/ddl_metadata/models/base.py:6-9`). New conversation contracts should reuse `ContractModel` or the same `ConfigDict(extra="forbid")`.
- Existing memory HTTP contracts are not tenant-safe: search requires `source` but no `user_id` (`src/data_agent/ddl_metadata/api/memories.py:24-36`), while get/history/update/delete accept only `memory_uid` (`src/data_agent/ddl_metadata/api/memories.py:44-93`).
- The repository has no `user_id`, conversation, message, summary, attachment, or `agent_id` runtime model. A full `rg` scan found only logging/error uses of the word “message”; there is no hidden conversation implementation to reuse.

### Existing MySQL and transaction behavior

- `MySQLDatabase.session()` creates an independent async Session, commits on normal exit, and rolls back/re-raises on failure (`src/data_agent/infrastructure/mysql.py:51-65`). It is the correct boundary for atomic message-plus-extraction-outbox writes.
- The application deliberately uses SQLAlchemy Core tables and caller-owned transactions, with one engine spanning the default `meta` schema and schema-qualified `data_agent` tables. Related contract: `.trellis/spec/backend/database-guidelines.md`.
- The current `data_agent` bootstrap owns exactly four memory tables: `agent_memory`, `agent_memory_event`, `agent_memory_link`, and `memory_index_outbox` (`docs/docker/mysql/data_agent.sql:10`, `:37`, `:52`, `:64`).
- `agent_memory` is DDL-shaped: required `source`, `schema_fingerprint`, and `created_job_id` (`docs/docker/mysql/data_agent.sql:13-25`). It has no tenant key or conversational provenance.
- `memory_index_outbox` is an index desired-state outbox keyed by `(memory_uid, target)` and foreign-keyed to an already-existing memory (`docs/docker/mysql/data_agent.sql:64-78`). It cannot represent “extract memory from this completed conversation round”; a small separate extraction outbox is required.
- Existing memory writes and index outbox writes already share the caller transaction. `MemoryRepository.upsert_candidates()` writes both desired index states (`src/data_agent/ddl_metadata/memory/mysql/repository.py:90`, `:280-288`), which is the path conversation extraction should ultimately reuse.
- No migration framework exists. `docs/docker/mysql/data_agent.sql` is idempotent bootstrap for empty volumes, not an upgrade mechanism. Existing installations will require an explicit reviewed migration command/script even if the repository continues to avoid introducing Alembic.

### Existing Mem0-style memory: reusable parts and hard gaps

Reusable as-is or with tenant parameters:

- MySQL remains authoritative; events and typed links preserve history; ES/Qdrant are rebuildable projections.
- `MemoryIndexOutboxRepository.set_desired_state()` and `claim_outbox()` provide atomic desired state, row locking, retry metadata, and acknowledgement (`src/data_agent/ddl_metadata/memory/mysql/index_outbox.py:34-113`).
- `MemoryIndexDispatcher.dispatch()` performs bounded ES/Qdrant projection and retry (`src/data_agent/ddl_metadata/memory/indexing/dispatcher.py:23-58`).
- `MemorySearchService.search()` concurrently runs BM25 and vector retrieval, fuses results, then re-reads and revalidates MySQL authority (`src/data_agent/ddl_metadata/memory/application/search.py:40-168`).
- The existing public surface already supplies search/get/history/update/delete semantics through `MemoryService` (`src/data_agent/ddl_metadata/memory/application/service.py:33-164`). Extend these operations with mandatory `user_id`; do not create a second generic memory API.
- Content hashing, content-addressed UIDs, projection versioning, append-only events, soft deletion, and index rebuild are reusable patterns.

Required gaps:

- `MemoryKind` only contains DDL semantic decisions, metric questions, user answers, and metric definitions (`src/data_agent/ddl_metadata/models/memory.py:21-28`). Conversation memory needs a small typed extension such as `USER_FACT`, `USER_PREFERENCE`, `USER_CONSTRAINT`, and `BUSINESS_RULE`.
- `MemoryContent`, `MemoryProjection`, and `MemoryCandidate` carry no `user_id` or conversation/message provenance (`src/data_agent/ddl_metadata/models/memory.py:123-170`).
- All authoritative reads are unsafe for tenant use: `get_by_uid()`, `get_many_active()`, and `history()` filter by UID alone (`src/data_agent/ddl_metadata/memory/mysql/repository.py:445-575`). Every public/read-after-index path must include `user_id`.
- Existing search isolation is `source` only in MySQL (`src/data_agent/ddl_metadata/memory/mysql/repository.py:514-533`), Elasticsearch (`src/data_agent/ddl_metadata/memory/indexing/elasticsearch.py:85-110`), Qdrant (`src/data_agent/ddl_metadata/memory/indexing/qdrant.py:30-49`, `:123-140`), and the final MySQL check (`src/data_agent/ddl_metadata/memory/application/search.py:155-158`). `user_id` must be a required filter in all four places.
- Existing update validation is deliberately DDL-specific and validates Meta object references (`src/data_agent/ddl_metadata/memory/application/service.py:93-137`, `:222`). Conversation fact correction needs a separate branch inside the same service/repository that writes an authoritative user-confirmed update/event and index outbox without requiring DDL reprocessing.
- `created_job_id` cannot honestly represent a conversation round. Add explicit provenance fields (`created_conversation_id`, `created_message_id` or a normalized origin type/id) rather than putting a message ID into a misleading DDL column.
- Current index mappings/payload indexes omit `user_id`; existing ES indices return early if already present, so merely changing mappings in code will not upgrade a live index. Projection version bump plus controlled rebuild/recreate is required.

### Existing worker, arq, and LLM capabilities

- `WorkerSettings` is the only arq discovery class and already schedules DDL dispatch, expiry, checkpoint cleanup, and memory-index outbox dispatch (`src/data_agent/ddl_metadata/worker/settings.py:17-48`). Add one bounded conversation-extraction maintenance function here; do not introduce Celery or another worker process.
- `dispatch_memory_index_outbox()` is already the scheduled projection entry point (`src/data_agent/ddl_metadata/worker/maintenance.py:52-55`). Extraction should finish by calling existing authoritative-memory/upsert behavior, leaving ES/Qdrant work to this dispatcher.
- Worker startup initializes MySQL, ES, Qdrant, TEI, the LLM, and Redis once (`src/data_agent/ddl_metadata/worker/lifecycle.py:35-76`). Conversation extraction needs no new external client.
- `LLMClient` supplies a reusable `ChatOpenAI` and probes structured output with the configured method (`src/data_agent/infrastructure/llm_client.py:18-60`). The installed client can do both normal `ainvoke()` chat completion and Pydantic `with_structured_output()` extraction; no new LLM SDK/dependency is needed.
- The current capability test only proves the mocked structured-output probe, not live free-text chat behavior (`tests/unit/infrastructure/test_llm_client.py:18-80`). Production readiness needs a deployment probe or a test fake for ordinary assistant generation, while CI should remain offline.
- The extraction outbox must not hold a database row lock during a potentially 60-second LLM call. Claim in a short transaction with a lease/token, commit, invoke the LLM, then finalize memory/event/index-outbox or record retry in a new transaction. The current index dispatcher holds its transaction during external calls; copying that literally would create avoidable contention for chat extraction.

### Smallest repository-faithful design

#### 1. Conversation authority

Add three MySQL tables in `data_agent`:

1. `agent_conversation`
   - `id BIGINT` internal PK; `uid CHAR(64)` public opaque ID.
   - `user_id VARCHAR(128) NOT NULL`.
   - `summary TEXT NULL`, `summary_through_message_id BIGINT NULL`.
   - `created_at`, `updated_at`.
   - Unique `uid`; index `(user_id, updated_at, id)`.
   - No `agent_id`, TTL, title, attachment metadata, or status machinery.

2. `agent_message`
   - `id BIGINT` chronological cursor; `uid CHAR(64)` public ID.
   - `user_id`, `conversation_id`, `turn_uid`.
   - role constrained in application and SQL to `user|assistant`.
   - plain `content TEXT`; `created_at`.
   - Unique `(user_id, conversation_id, turn_uid, role)` for retry idempotency.
   - Index `(user_id, conversation_id, id)`.
   - Keep `user_id` redundantly on the row so every query has an explicit tenant predicate; validate it matches the parent conversation in the repository transaction.

3. `conversation_memory_outbox`
   - One row per completed turn: `turn_uid` PK, `user_id`, `conversation_id`, `user_message_id`, `assistant_message_id`.
   - Claim/retry fields: `attempts`, `available_at`, `lease_token`, `lease_expires_at`, `last_error_type`, `created_at`, `updated_at`.
   - The unique turn key makes repeated request delivery unable to create repeated extraction work.

No conversation data belongs in Redis. Redis remains the existing DDL job/checkpoint store and keeps its current 86,400-second checkpoint retention (`conf/app_config.yaml:50`).

#### 2. Round write path

Use one public `POST /api/v1/users/{user_id}/conversations/{conversation_uid}/turns` contract with a client-generated or server-returned idempotency `turn_uid` and pure-text `content`.

Minimal safe sequence:

1. In a short MySQL transaction, verify `(user_id, conversation_uid)` and insert the `user` message idempotently.
2. Load bounded context using the same `user_id`: stored summary, recent messages, and relevant long-term memory searched by the current input.
3. Call the existing `ChatOpenAI.ainvoke()` for the assistant response.
4. In one MySQL transaction, lock/recheck the conversation, insert the assistant message, update conversation time, and insert `conversation_memory_outbox`. Commit before returning the assistant response.
5. A retry with the same `turn_uid` returns the existing complete pair. A retry finding only the durable user message may regenerate the assistant, but the unique assistant row prevents duplicates.

This keeps user input durable even if the LLM fails, while a round is only reported complete after the assistant message and extraction outbox commit. It also satisfies the requirement that extraction/index failures never roll back messages.

#### 3. History and bounded context

- History API: keyset pagination by message `id`, always filtered by both `user_id` and `conversation_id`. Query newest `limit + 1`, reverse the returned page into chronological order, and return the next older cursor. Avoid offset drift in actively growing conversations.
- Context assembler: read `summary` up to `summary_through_message_id`, then a configured maximum count/character budget of messages after that cursor, relevant user memory, and the current input. Do not expose hidden prompts or reasoning.
- The extraction worker can update summary and `summary_through_message_id` from the same ordered message prefix it processed. Use compare-and-set on `summary_through_message_id` so out-of-order workers cannot move the summary backward.
- Full history remains independently pageable from MySQL; old messages are not copied into ES/Qdrant.

#### 4. Conversation-to-memory extraction

- Add a strict Pydantic extraction result whose candidates contain: kind, normalized statement, stable scope key, `supporting_user_quote`, optional `confirmed_assistant_message_id`, and the source user/assistant message IDs.
- For a direct fact/preference/constraint/rule, require `supporting_user_quote` to be an exact non-empty substring of the user message.
- For an assistant-originated conclusion, require both an assistant quote/message ID and an exact user confirmation quote from a later user message. Reject candidates that only cite assistant text.
- The prompt must instruct the model to return no candidate for advice, speculation, inferred demographics, or ambiguous assent. Code validation of provenance is still mandatory; prompt wording alone is not a trust boundary.
- Convert accepted extraction candidates into the existing typed `MemoryCandidate`/event/link/index-outbox flow with `trust=user_confirmed`, `user_id`, and conversation/message provenance. Content-addressing plus unique tenant/content keys makes retries idempotent.
- Use a stable conversational source such as `data_agent_conversation`; do not use `conversation_id` as source because memory must cross conversations. `user_id` is the tenant boundary, and `source` remains a semantic producer/source label.

#### 5. Deletion

- Conversation deletion: hard-delete only `(user_id, conversation_uid)` messages/outbox/conversation in one transaction. Do not delete shared memory.
- Memory deletion: reuse the existing audited soft-delete plus ES/Qdrant DELETE outbox, now requiring `user_id`.
- User deletion: first make all user conversations and memories unavailable atomically (delete conversations/messages and tombstone memories), enqueue projection deletes, then physically purge memory/event/link rows only after both index targets acknowledge deletion. The existing `memory_index_outbox` foreign key to `agent_memory` (`docs/docker/mysql/data_agent.sql:78`) prevents reliable immediate hard deletion before projection cleanup.
- A synchronous “hard delete authority and also reliably retry derived-index deletion” is not supported by the current schema. Tombstone-then-purge is the smallest safe extension.

### Concrete affected files

New feature files (small cohesive package):

- `src/data_agent/conversation/__init__.py` — side-effect-free package marker.
- `src/data_agent/conversation/models.py` — strict conversation/message/turn/history/context/extraction contracts.
- `src/data_agent/conversation/api.py` — create/list/delete conversation, submit turn, paginate messages, bounded-context, and user deletion endpoints.
- `src/data_agent/conversation/service.py` — round orchestration, context assembly, deletion, and transaction boundaries.
- `src/data_agent/conversation/mysql_tables.py` — the three new SQLAlchemy Core tables.
- `src/data_agent/conversation/repository.py` — tenant-filtered conversation/message/outbox queries and idempotent writes.
- `src/data_agent/conversation/extraction.py` — structured extraction, provenance validation, summary compare-and-set, and conversion to existing memory candidates.

Existing files that must change:

- `docs/docker/mysql/data_agent.sql` — add conversation/message/extraction-outbox tables; add `user_id` and conversational provenance to authoritative memory; add tenant-aware indexes/constraints.
- `src/data_agent/application.py` — construct conversation service and include its router.
- `src/data_agent/settings.py`, `conf/app_config.yaml`, `tests/unit/test_settings.py` — bounded recent-message/context sizes, extraction batch/lease/backoff, and summary thresholds. Do not add retention settings because data is permanent by requirement.
- `src/data_agent/ddl_metadata/models/memory.py` — conversational kinds/content and tenant/provenance fields on memory detail/candidate/projection.
- `src/data_agent/ddl_metadata/memory/domain/payloads.py` — deterministic text/object extraction for new content types.
- `src/data_agent/ddl_metadata/memory/mysql/tables.py` — tenant/provenance columns.
- `src/data_agent/ddl_metadata/memory/mysql/repository.py` — mandatory `user_id` on every public lookup/mutation and tenant-aware uniqueness.
- `src/data_agent/ddl_metadata/memory/mysql/index_outbox.py` — tenant-bearing projections; deletion/purge coordination if physical user deletion is implemented.
- `src/data_agent/ddl_metadata/memory/application/search.py` — mandatory tenant filter through exact baseline, both derived indexes, and final MySQL authority check.
- `src/data_agent/ddl_metadata/memory/application/service.py` and `src/data_agent/ddl_metadata/api/memories.py` — mandatory `user_id`; conversation-content update validation without DDL Meta references.
- `src/data_agent/ddl_metadata/memory/indexing/elasticsearch.py` and `qdrant.py` — store/index/filter `user_id`.
- `src/data_agent/ddl_metadata/worker/maintenance.py`, `settings.py`, and `lifecycle.py` — extraction cron and shared conversation worker dependency construction.
- `tests/helpers/fakes.py` / `tests/helpers/factories.py` — deterministic chat/extraction fakes and tenant-aware memory factories.
- New focused tests under `tests/unit/conversation/`, `tests/integration/persistence/test_conversation_repository.py`, `tests/integration/test_conversation_api.py`, and tenant/deletion additions to existing memory integration tests.

Files that do not need a new implementation:

- `src/data_agent/infrastructure/mysql.py` — existing managed transaction behavior is sufficient.
- `src/data_agent/infrastructure/llm_client.py` — existing `ChatOpenAI` supports normal and structured async invocation; only tests/capability expectations may need extension.
- `src/data_agent/infrastructure/checkpoint_store.py` and DDL graph state/node files — conversation history must not enter LangGraph checkpoint state.
- ES/Qdrant client managers and TEI client — reuse unchanged.
- `pyproject.toml` / `uv.lock` — no new runtime dependency is required.

### Minimum validation matrix

- Contracts reject unknown fields, empty/oversized text, roles other than `user|assistant`, and attachment/multimodal payloads.
- Same `user_id` can create multiple isolated conversations; history is chronological and keyset-paged after 24 hours with Redis/checkpoints removed.
- Another `user_id` receives not-found for conversation UID, message cursor, memory UID, history, update, delete, and search.
- Same `turn_uid` creates one user row, one assistant row, and one extraction outbox row.
- Assistant persistence failure returns failure and never reports the round complete.
- Extraction/LLM/TEI/ES/Qdrant failure leaves messages readable and schedules retry.
- A direct user fact is accepted; an assistant guess is rejected; a later explicit user confirmation accepts the assistant conclusion.
- Context remains within configured count/character bounds and contains summary, recent messages, relevant same-user memory, and current input.
- Conversation delete removes only its timeline/outbox; shared memory remains.
- User deletion immediately prevents recall and eventually removes both projection targets before authority purge.
- Existing DDL interrupt/resume/checkpoint tests continue unchanged, proving Redis remains task-only.
- Existing memory repository test already covers ADD idempotency, history, soft deletion, and dual-target outbox (`tests/integration/persistence/test_memory_repository.py:24-104`); extend it with tenant collision and tenant-filter checks rather than duplicating those cases.

### Risks and missing capabilities

- **Tenant migration:** existing memory rows have no `user_id`. A deterministic reserved owner/backfill policy is required before making the column non-null. The repository has no migration tool, and bootstrap SQL will not update initialized volumes automatically.
- **Breaking API contract:** adding mandatory `user_id` to existing memory APIs is necessary for isolation but breaks current callers. Because the service is loopback/local and unreleased contracts are repository-internal, a hard contract migration is smaller and safer than keeping an unscoped compatibility route.
- **UID collision/leak:** filtering only after `get_by_uid()` is insufficient. Tenant scope must participate in repository predicates and preferably conversational-memory UID generation/unique keys.
- **Index upgrade:** existing ES/Qdrant setup returns early for an existing index/collection. New `user_id` mappings and payload indexes require a projection-version bump and explicit rebuild/recreate path.
- **Deletion ordering:** the current index-outbox foreign key requires authority to remain until deletes are acknowledged. Immediate physical user-memory deletion would lose reliable cleanup.
- **Confirmation semantics:** exact-quote provenance prevents unsupported assistant-only candidates, but detecting whether “yes” explicitly confirms a specific prior statement still depends on structured extraction. Keep the candidate schema narrow, require linked message IDs/quotes, and test ambiguous assent as rejection.
- **Concurrent turns:** either serialize turns per conversation with a MySQL row lock/short application lease or define ordering by committed message ID. Without this, assistant contexts can race. The smallest first version should reject a second in-flight turn for the same conversation.
- **Summary freshness:** asynchronous extraction means summary may lag. Context must safely fall back to recent raw messages after `summary_through_message_id`; summary failure cannot block chat.
- **Long text:** `TEXT` is adequate but request and context character limits are missing today. Add explicit Pydantic and configuration bounds; never rely on model context overflow errors.
- **No live LLM proof:** current tests use fakes/mocks, and the remembered prior implementation also did not run a real external-LLM capability probe. Do not claim production chat/extraction compatibility without a deployment probe.

### External references and versions

- No external Mem0 package is installed or needed. “Mem0-style” is already implemented locally as MySQL authority + events/links + desired-state outbox + hybrid derived projections.
- Repository dependency baseline from `pyproject.toml`: Python `>=3.13,<3.14`, FastAPI `>=0.139,<0.140`, Pydantic `>=2.12.5`, SQLAlchemy `>=2.0.51`, asyncmy `>=0.2.11`, arq `>=0.28,<0.29`, LangChain OpenAI `>=1.3,<1.4`, Elasticsearch async `>=8.19,<9`, Qdrant client `>=1.18.0`, Redis `>=5.2.1,<6`.
- MySQL bootstrap targets MySQL 8.4 through `docs/docker/docker-compose.yml`; all new tables should remain InnoDB so conversation/message/outbox commits are atomic.

### Related specs

- `.trellis/spec/backend/database-guidelines.md` — shared engine, caller-owned transaction, Core tables, cross-schema atomicity, memory authority/outbox, no migration framework.
- `.trellis/spec/backend/directory-structure.md` — feature-first packages, application composition root, deterministic domain vs technology adapters, side-effect-free `__init__.py`.
- `.trellis/spec/backend/external-service-integrations.md` — managed LLM/TEI/ES/Qdrant clients, bounded failures, and offline tests.
- `.trellis/spec/backend/error-handling.md` — preserve rollback and external failures; safe HTTP envelopes.
- `.trellis/spec/backend/quality-guidelines.md` — strict settings descriptions, fakes instead of live CI LLM, integration markers, and repository validation commands.
- `.trellis/spec/guides/code-reuse-thinking-guide.md` — extend the existing memory pipeline rather than create a second generic memory stack.
- `.trellis/spec/guides/cross-layer-thinking-guide.md` — trace tenant/provenance fields across HTTP, Pydantic, MySQL, outbox, ES, Qdrant, and tests.

## Caveats / Not Found

- No authentication/user directory exists; `user_id` remains a caller-supplied isolation key until a trusted loopback gateway or future auth layer supplies it. The API must not be exposed beyond loopback in that state.
- No production migration mechanism was found. `CREATE TABLE IF NOT EXISTS` bootstrap alone cannot safely add non-null tenant columns or indexes to existing tables.
- No current conversation/chat contracts, tables, history API, summarizer, extraction outbox, or user-deletion workflow were found.
- No message vector index or attachment stack exists, which matches the explicit first-version exclusions.
- This research did not contact an external LLM, MySQL, Redis, ES, Qdrant, or TEI service; findings are source/spec based.
