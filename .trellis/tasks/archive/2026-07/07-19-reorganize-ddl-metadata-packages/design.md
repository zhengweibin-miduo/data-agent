# Package Responsibility Refactor Design

## Design Goal

Make package paths answer two questions without opening a file:

1. Which feature or runtime boundary owns this code?
2. Is it deterministic domain logic, application orchestration, or a
   technology-specific implementation?

The refactor is behavior-preserving and uses a hard internal import migration.

## Target Package Shape

```text
src/data_agent/
├── application.py
├── logging.py
├── main.py
├── settings.py
├── infrastructure/
│   ├── checkpoint_store.py
│   ├── elasticsearch.py
│   ├── llm_client.py
│   ├── mysql.py
│   ├── qdrant.py
│   ├── redis.py
│   └── tei_embeddings.py
└── ddl_metadata/
    ├── api/
    │   ├── jobs.py
    │   ├── memories.py
    │   └── router.py
    ├── jobs/
    │   ├── identifiers.py
    │   ├── store.py
    │   └── redis/
    │       ├── base.py
    │       ├── codec.py
    │       ├── keys.py
    │       ├── lease_store.py
    │       ├── outbox_store.py
    │       ├── scripts.py
    │       └── state_store.py
    ├── memory/
    │   ├── application/
    │   │   ├── context.py
    │   │   ├── search.py
    │   │   ├── service.py
    │   │   └── snapshots.py
    │   ├── domain/
    │   │   ├── candidates.py
    │   │   ├── payloads.py
    │   │   └── ranking.py
    │   ├── indexing/
    │   │   ├── dispatcher.py
    │   │   ├── elasticsearch.py
    │   │   ├── qdrant.py
    │   │   └── rebuilder.py
    │   └── mysql/
    │       ├── index_outbox.py
    │       ├── repository.py
    │       └── tables.py
    ├── models/
    ├── persistence/
    │   ├── metadata_repository.py
    │   ├── schema.py
    │   └── tables.py
    ├── workflow/
    │   ├── contracts.py
    │   ├── graph.py
    │   ├── llm_metadata_generator.py
    │   ├── nodes.py
    │   ├── routing.py
    │   └── state.py
    ├── worker/
    │   ├── job_runner.py
    │   ├── lifecycle.py
    │   ├── maintenance.py
    │   └── settings.py
    ├── errors.py
    ├── identifiers.py
    ├── parsing.py
    └── validation.py
```

Every package `__init__.py` contains only a package docstring. Consumers import
from the concrete owning module.

## Dependency Direction

```text
main/application
  -> api/router + jobs/store + memory/application
  -> infrastructure lifecycle

worker/settings
  -> worker/job_runner + worker/maintenance + worker/lifecycle

worker/job_runner
  -> jobs/store + workflow contracts/state

workflow/graph
  -> workflow nodes/routing/contracts/state

workflow/nodes
  -> memory/domain + parsing + validation + models

memory/application
  -> memory/domain + memory/mysql + memory/indexing + persistence metadata

jobs/store
  -> jobs/redis

technology-specific packages
  -> models/settings/infrastructure clients only
```

Domain modules do not import FastAPI, arq, initialized clients, SQLAlchemy
Sessions, Elasticsearch, Qdrant, TEI, or Redis.

## Jobs Design

`DDLJobStore` remains the application-facing facade to avoid exposing several
Redis collaborators to API and worker code. Its implementation composes
Redis-specific stores from `jobs/redis/`. Pure `question_set_id()` moves to
`jobs/identifiers.py` so workflow code no longer imports a storage facade for a
hash calculation.

Redis primitives retain the `Store` suffix where the class persists state.
Pure helpers are renamed atomically with all usages and tests:
`JobKeysStore` becomes `JobKeys`, `JobCodecStore` becomes `JobCodec`, and
`JobScriptsStore` becomes `JobScripts`.

## Memory Design

### Domain

Pure canonicalization, hashes, projection text, candidate construction, and
rank fusion live under `memory/domain/`. They accept and return typed models
and are independently unit-testable.

### Application

Context loading, search orchestration, browser management, and accepted
snapshot persistence own use-case transaction boundaries. They may initialize
transaction-scoped repositories but not define SQL or external-index payload
formats.

### MySQL

Memory table definitions, row decoding, authoritative record/history/link
operations, and index-outbox persistence live under `memory/mysql/`.
`MemoryRepository` owns authoritative records, history, links, exact queries,
and browser mutations. `MemoryIndexOutboxRepository` owns desired index state,
claiming, retry, acknowledgement, projection reads, and rebuild scans.
`MemoryRepository` receives or constructs the outbox repository for atomic
record-plus-outbox writes on the same Session; indexing services use the
focused outbox repository directly.

All memory and Meta table definitions import one `MetaData` instance from
`ddl_metadata/persistence/schema.py`. This preserves integration
`metadata.create_all()` and one cross-database MySQL transaction.

### Indexing

Elasticsearch and Qdrant projections are separate adapters. Dispatcher and
rebuilder are separate runtime use cases because one synchronizes pending
desired state while the other recreates and repopulates derived indexes.

## API Design

Job and memory routers live in separate modules and attach to one aggregate
router. Route paths, methods, response models, status codes, query constraints,
state dependency lookup, and logging fields remain unchanged.

## Workflow Design

The state schema and dependency protocols become importable without building a
graph. `DDLWorkflowNodes` binds `DDLGraphDependencies` once and exposes node
methods. Routing functions stay pure. `graph.py` registers the same node names
and edge topology and compiles with the provided checkpointer.

This shape allows unit tests to cover nodes and routes without relying on a
657-line closure while keeping LangGraph checkpoint state fully compatible.

## Worker Design

The worker package separates:

- a single DDL job execution/recovery unit;
- maintenance jobs that arq schedules;
- resource lifecycle and graph construction;
- declarative arq discovery settings.

`WorkerSettings` remains the only arq discovery class. Its import path changes,
but every registered function name and schedule remains unchanged.

## Retained Modules

The audit intentionally keeps root composition, settings, logging, shared
infrastructure adapters, models, parsing, validation, identifiers, errors, and
Meta persistence cohesive. File size is not treated as evidence of mixed
ownership.

## Compatibility Contract

The following must be compared before and after:

- FastAPI route table: path, method, name, status code, response model.
- `WorkerSettings`: function names, cron names/schedules, startup/shutdown
  callbacks, Redis settings, concurrency, timeouts, retry, and result retention.
- Redis keys, hash fields, canonical JSON, Lua scripts, transition outcomes,
  arq enqueue name, and checkpoint cleanup members.
- SQLAlchemy table names, columns, schemas, constraints/defaults, statement
  behavior, and shared transaction scope.
- LangGraph state keys, node names, edges, interrupt projection, thread ID,
  durability, and checkpoint version guard.
- Pydantic models, YAML keys/defaults/validation, log component/event/field
  names, and public error payloads.

## Migration Strategy

1. Add new packages and move cohesive code without changing logic.
2. Split pure helpers and dependency protocols.
3. Update production imports in dependency order.
4. Update tests and current specs.
5. Delete retired files after stale-import searches pass.

No compatibility modules remain at retired internal paths. Archived Trellis
tasks and journals are not rewritten.

## Rollback

Each implementation phase ends with compile, import, and focused-test checks.
If a phase fails, revert only that phase's uncommitted file moves and import
updates; Redis, MySQL, and checkpoint data need no rollback because no external
format or schema migration is permitted.
