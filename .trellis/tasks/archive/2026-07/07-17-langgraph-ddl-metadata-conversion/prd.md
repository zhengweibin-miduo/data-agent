# LangGraph DDL Metadata Conversion

## Goal

Provide a local asynchronous HTTP API that converts MySQL `CREATE TABLE` DDL
into validated fact-table, dimension-table, column, and user-confirmed metric
metadata, then synchronizes the result to the existing Meta MySQL schema
without partial writes. Application-owned long-term memory is stored in a
separate configurable MySQL database while sharing the same transaction.

## Background

- The browser calls the API directly from the same development machine.
- The first release is local-only, has no user authentication, and must not be
  exposed as an unauthenticated LAN or public service.
- The repository already owns an async SQLAlchemy MySQL manager and the local
  Meta schema, but it has no HTTP framework, Redis integration, chat-model
  client, DDL parser, repository layer, or production persistence queries.
- The existing Meta schema consists of `table_info`, `column_info`,
  `metric_info`, and `column_metric`.
- Long-term memory is application state, not Meta business metadata, and must
  not create tables in the Meta database.
- `table_info.role` stores `fact` or `dim`; `column_info.role` stores
  `primary_key`, `foreign_key`, `measure`, or `dimension`.

## Requirements

### R1. Local asynchronous API

- `POST /api/v1/metadata/ddl-jobs` accepts a bounded DDL request and immediately
  returns `202 Accepted` with an opaque job ID.
- `GET /api/v1/metadata/ddl-jobs/{job_id}` returns the current status,
  questions, result, or structured error.
- `POST /api/v1/metadata/ddl-jobs/{job_id}/answers` submits answers for the
  current question round and resumes the same LangGraph execution.
- Public job states are `pending`, `running`, `waiting_input`, `succeeded`,
  `rejected`, and `failed`.
- The server binds to `127.0.0.1` by default. CORS accepts only explicitly
  configured local frontend origins.
- Authentication, user ownership, and LAN/public deployment are not part of
  the first release.

### R2. Supported DDL

- Accept MySQL `CREATE TABLE` statements, including multiple tables, primary
  keys, foreign keys, and table/column `COMMENT` clauses.
- Reject `ALTER TABLE`, `DROP TABLE`, `CREATE VIEW`, DML, non-MySQL dialects,
  malformed input, and configured input-limit violations.
- Use a deterministic SQL AST parser for names, types, constraints, and
  comments. The LLM must not reconstruct physical schema facts.
- `column_info.role = foreign_key` is sufficient for the first release.
  Referenced target table/column metadata is not persisted.

### R3. Semantic conversion

- Use LangGraph to coordinate parsing, semantic classification, deterministic
  validation, bounded correction, metric clarification, and persistence.
- Use an OpenAI-compatible chat model configured by `base_url` and `model`.
  Read its API key only from a server-side environment variable.
- Use Pydantic structured output for every LLM response.
- The LLM may classify tables as `fact` or `dim`, classify non-structural
  columns as `measure` or `dimension`, and propose descriptions and aliases.
- Primary-key and declared foreign-key roles come from the parsed DDL and
  cannot be overridden by the LLM.
- Reject hallucinated tables/columns, role conflicts, unresolved validation
  errors, and low-confidence semantic output instead of writing it.

### R4. Human-in-the-loop metric generation

- After table and column metadata passes validation, the LLM generates
  structured questions needed to define business metrics.
- LangGraph pauses with `interrupt`; the job enters `waiting_input` and the
  frontend obtains the questions through the status API.
- The user has 30 minutes to answer each round. At most two clarification
  rounds are allowed.
- Incomplete answers may produce one follow-up round. Expiry or unresolved
  ambiguity after round two rejects the job and writes no Meta rows.
- The LLM generates `metric_info` and `column_metric` only from validated
  columns and explicit user answers. It must not invent missing business
  definitions.

### R5. Redis execution and recovery

- Redis owns the async queue, job status projection, and durable LangGraph
  checkpoints. MySQL is not used as a job queue.
- A worker executes jobs with bounded concurrency and prevents duplicate
  execution of the same job revision.
- Redis persistence and a named local volume preserve accepted jobs and graph
  checkpoints across API/worker restarts.
- Transient LLM and MySQL failures use bounded retry with backoff.
- A retry resumes from the last durable graph checkpoint. In particular, a
  MySQL persistence failure must retry persistence without repeating completed
  LLM nodes.
- A worker crash after MySQL commit but before checkpoint acknowledgement is
  safe because persistence is idempotent.
- Expired `waiting_input` checkpoints are cleaned up while a terminal rejected
  job result remains queryable for a bounded retention period.

### R6. Atomic and idempotent Meta synchronization

- No Meta business rows are written while the job is `waiting_input`.
- After every table, column, metric, and relation validates, synchronize all
  four Meta tables in one managed MySQL transaction.
- Derive stable IDs deterministically from the logical source and qualified
  object names.
- Repeating the same accepted request produces the same database state.
- For tables included in the submitted DDL, upsert the current snapshot,
  delete stale columns and column/metric associations, and remove metrics that
  become orphaned.
- Tables absent from the submitted DDL and their metadata remain unchanged.
- Any persistence exception rolls back the full synchronization and preserves
  the original database exception for retry/error classification.
- Meta tables and application memory tables use schema-qualified statements on
  the same MySQL engine and Session so one InnoDB transaction remains atomic
  across the two configured databases.

### R7. Efficiency and observability

- Avoid LLM calls for information available from the AST or deterministic
  rules.
- Group related tables so the model sees the relevant fact/dimension context;
  cap model-call concurrency and input size.
- Include the canonical DDL hash, model name, prompt/schema version, and job ID
  in retry/cache identity so stale model output is not reused accidentally.
- Bind the job ID as Loguru `trace_id` across API, worker, graph, and
  persistence logs.
- Do not log DDL bodies, user answers, API keys, tokens, or complete database
  URLs.
- Return stable error codes, stage, retryability, attempt count, and safe
  details to the frontend.

### R8. LLM memory

- Treat LLM memory as an application feature, not merely as queue retry state,
  and adapt the canonical-record/derived-payload/typed-relation architecture
  used by `usememos/memos`.
- Redis/LangGraph checkpoints hold the active job's working memory: parsed
  schema, semantic decisions, validation issues, question rounds, user
  answers, attempts, and the next executable node.
- MySQL stores successful long-term memories as stable-UID records with a
  canonical structured content document, `NORMAL`/`ARCHIVED` state, optional
  pinning, timestamps, and a rebuildable JSON payload.
- `llm_memory` and `llm_memory_relation` live in the configurable application
  database (`data_agent` by default), never in the Meta database.
- The rebuildable payload contains retrieval tags, source/table/column
  identities, schema fingerprints, trust level, and model/prompt/graph
  versions. Canonical content remains the source of truth.
- Store typed memory relations separately with a unique
  `(memory_id, related_memory_id, relation_type)` key. Required relation types
  are `REFERENCE`, `COMMENT`, and `SUPERSEDES`.
- User answers are linked as `COMMENT` memories to the LLM question; accepted
  semantic/metric memories reference their inputs and supersede older active
  decisions rather than silently overwriting history.
- Default retrieval includes only compatible `NORMAL` memories. Pinned,
  user-confirmed memories rank above model-only memories.
- Expose browser-facing APIs to list and inspect bounded memory records,
  archive active memories, pin or unpin active memories, and submit structured
  corrections.
- List APIs default to `NORMAL` records and support bounded pagination plus
  source, kind, status, and pinned filters. Detail APIs expose canonical
  content, derived payload, and typed relations without exposing prompts,
  chain-of-thought, secrets, or unbounded transcripts.
- Archive and pin operations affect future memory retrieval only. They do not
  silently rewrite the already accepted Meta snapshot.
- Corrections are allowed only for active semantic decisions and metric
  definitions. A correction creates a user-confirmed memory, links it with
  `SUPERSEDES`, and archives the replaced memory atomically; question and
  answer audit records remain immutable.
- A correction is applied to Meta only when the logical source is submitted
  through the DDL workflow again and the replacement passes current AST and
  deterministic validation. The correction response must state that
  reprocessing is required.
- Content/payload extraction must be rerunnable in bounded batches so derived
  tags and indexes can be rebuilt after schema or extraction-version changes.
- Memory records must be structured and versioned by source, schema
  fingerprint, model, prompt/schema version, and graph version.
- Never persist hidden chain-of-thought. Persist only validated decisions,
  concise evidence, explicit user answers, validation outcomes, and recovery
  metadata needed by the application.
- Failed, rejected, incomplete, or expired outputs must not become trusted
  long-term semantic memory.
- Stale, corrupt, structurally incompatible, or version-incompatible memory
  must be ignored or revalidated rather than overriding current DDL facts.
- Successful Meta synchronization and creation/supersession of its long-term
  memories occur in one MySQL transaction. Failed/rejected jobs leave neither
  Meta rows nor trusted memories.
- Retrieve by exact source/object/schema fingerprints first. A vector index is
  not required in the first release; if later added, it is a rebuildable
  projection rather than the source of truth.

## Acceptance Criteria

- [ ] A valid multi-table MySQL DDL sample produces schema-valid table and
      column metadata without adding or renaming physical objects.
- [ ] Unsupported statements and malformed DDL end as `rejected` with no Meta
      writes and without calling the LLM when parsing cannot succeed.
- [ ] The submission endpoint returns `202` before conversion completes, and
      the status endpoint exposes every required public state.
- [ ] A fact-table sample pauses in `waiting_input`, exposes structured metric
      questions, resumes with matching answers, and produces valid
      `metric_info` and `column_metric` rows.
- [ ] No Meta rows exist for a job before metric clarification completes.
- [ ] An expired or twice-unresolved clarification rejects the job after the
      configured 30-minute rounds and leaves all four Meta tables unchanged.
- [ ] Hallucinated entities, role conflicts, unknown metric columns, and
      low-confidence output cannot reach persistence.
- [ ] A forced MySQL failure rolls back all four tables; retry resumes at
      persistence without making another LLM call.
- [ ] A forced write failure in the schema-qualified application memory
      database rolls back Meta writes made earlier in the same transaction.
- [ ] The Meta database owns only `table_info`, `column_info`, `metric_info`,
      and `column_metric`; memory tables are qualified to a different validated
      database identifier.
- [ ] A repeated successful import is idempotent. A changed import removes
      stale metadata only for tables included in that import.
- [ ] API/worker restart preserves queued, running/checkpointed, and
      `waiting_input` jobs under the documented Redis persistence assumptions.
- [ ] Duplicate answer submissions or stale question rounds do not resume the
      graph twice.
- [ ] Active-job memory survives the documented API/worker restart scenarios
      and resumes from the correct graph node without losing validated state.
- [ ] No failed/rejected model output or hidden chain-of-thought is stored as
      reusable semantic memory.
- [ ] A repeated compatible DDL job reuses validated `NORMAL` memory, asks only
      for missing/changed business meaning, and still revalidates physical
      facts against the current AST.
- [ ] A corrected decision creates a new memory, archives/supersedes the old
      one, and preserves an auditable typed relation without returning both as
      active context.
- [ ] Browser APIs can list and inspect bounded active or archived memories
      and never expose raw prompts, hidden reasoning, secrets, or unbounded
      transcripts.
- [ ] Archiving removes a memory from default retrieval; pinning and unpinning
      are idempotent and affect precedence only for compatible active memory.
- [ ] A valid correction atomically creates a user-confirmed replacement,
      archives/supersedes the previous active memory, and reports that the
      source must be reprocessed before Meta changes.
- [ ] Invalid, immutable-kind, stale, archived, or source-busy memory mutations
      return a stable error without partially changing memory or Meta rows.
- [ ] Derived memory payloads can be rebuilt from canonical content in bounded
      batches without changing accepted semantic meaning.
- [ ] The default server does not listen on non-loopback interfaces, rejects
      browser Origins outside the configured allowlist, and never exposes
      server-side secrets.
- [ ] Repository-native lock, lint, type, compile, configuration, focused unit,
      and live MySQL/Redis integration checks pass when their services are
      available.

## Out of Scope

- Authentication, user/task ownership, multi-tenant isolation, and LAN/public
  deployment.
- Automatic creation or modification of source warehouse tables.
- Executing SQL generated by the LLM.
- Persisting foreign-key target table/column relationships.
- `ALTER`, `DROP`, views, DML, or non-MySQL dialects.
- A CLI entry point.
- A MySQL job table or Celery.
- Vector similarity search for long-term memory.
- Hard deletion, bulk memory editing, arbitrary note content, user-facing note
  comments, attachments, reactions, and other Memos product features.
