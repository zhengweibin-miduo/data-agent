# Backend Directory Structure

## Current Scope

The installable Python application is independently owned by `backend/`.
Runtime code lives directly under `backend/src/` and is organized by business
feature first; there is no `data_agent` package or replacement umbrella namespace.
Shared Pydantic contracts live under `models`, long-term memory is a root
business module used by both DDL metadata and Conversation, and shared
external-resource lifecycles live under `infrastructure`. DDL metadata owns its
transport, job orchestration, accepted Meta Snapshot publication, rebuildable
Meta Projection, workflow, and worker entry points.

## Directory Layout

```text
backend/src/
├── application.py
├── app_logging.py
├── main.py
├── settings.py
├── errors.py
├── identifiers.py
├── models/
│   ├── base.py
│   ├── jobs.py
│   ├── memory.py
│   ├── physical.py
│   └── semantic.py
├── persistence/
│   └── schema.py
├── memory/
│   ├── adapters/
│   │   ├── composition.py
│   │   ├── mysql.py
│   │   ├── projection_indexes.py
│   │   └── search_indexes.py
│   ├── application/
│   │   ├── contracts.py
│   │   ├── index_dispatcher.py
│   │   ├── maintenance.py
│   │   ├── search.py
│   │   └── service.py
│   ├── domain/
│   │   ├── candidates.py
│   │   ├── lifecycle.py
│   │   ├── payloads.py
│   │   ├── policies.py
│   │   └── ranking.py
│   ├── indexing/
│   │   ├── elasticsearch.py
│   │   ├── qdrant.py
│   │   └── rebuilder.py
│   └── mysql/
│       ├── index_outbox.py
│       ├── repository.py
│       └── tables.py
├── infrastructure/
│   ├── checkpoint_store.py
│   ├── elasticsearch.py
│   ├── llm_client.py
│   ├── mysql.py
│   ├── qdrant.py
│   ├── redis.py
│   └── tei_embeddings.py
├── conversation/
│   ├── api.py
│   ├── application/
│   │   ├── contracts.py
│   │   ├── extraction.py
│   │   └── service.py
│   ├── adapters/
│   │   ├── extraction_model.py
│   │   ├── long_term_memory.py
│   │   └── mysql/
│   │       ├── extraction.py
│   │       ├── store.py
│   │       └── user_data.py
│   ├── models.py
│   ├── mysql_tables.py
│   └── repository.py
├── answer_readiness/
│   ├── classifier.py
│   ├── models.py
│   ├── service.py
│   └── tool.py
├── data_sync/
│   ├── application/
│   │   ├── contracts.py
│   │   └── service.py
│   ├── adapters/
│   │   ├── composition.py
│   │   ├── mysql.py
│   │   └── source.py
│   ├── backfill.py
│   ├── binlog.py
│   ├── models.py
│   ├── repository.py
│   ├── schema_sync.py
│   ├── tables.py
│   └── worker.py
└── ddl_metadata/
    ├── application/
    │   └── accepted_snapshot.py
    ├── adapters/
    │   └── mysql/
    │       └── accepted_snapshot.py
    ├── api/
    │   ├── job_events.py
    │   ├── jobs.py
    │   ├── memories.py
    │   └── router.py
    ├── jobs/
    │   ├── identifiers.py
    │   ├── store.py
    │   └── redis/
    │       ├── base.py
    │       ├── codec.py
    │       ├── event_store.py
    │       ├── keys.py
    │       ├── lease_store.py
    │       ├── outbox_store.py
    │       ├── scripts.py
    │       └── state_store.py
    ├── persistence/
    │   ├── memory_references.py
    │   ├── metadata_repository.py
    │   └── tables.py
    ├── meta_projection/
    │   ├── application/
    │   ├── adapters/
    │   ├── domain.py
    │   └── tables.py
    ├── workflow/
    │   ├── contracts.py
    │   ├── graph.py
    │   ├── llm_metadata_generator.py
    │   ├── memory_context.py
    │   ├── nodes.py
    │   ├── routing.py
    │   └── state.py
    ├── worker/
    │   ├── job_runner.py
    │   ├── lifecycle.py
    │   ├── maintenance.py
    │   └── settings.py
    ├── parsing.py
    └── validation.py
```

Tests mirror deterministic package boundaries under `backend/tests/unit/`;
integration tests remain scenario-oriented under `backend/tests/integration/`.
Configuration stays under `backend/conf/`, while local service definitions and
SQL bootstrap assets stay under the repository-owned `docs/docker/`.

## Ownership

- `application` is the FastAPI composition root and lifecycle owner.
- `conversation` owns permanent text conversations, turn
  idempotency, bounded context, and the leased conversation-memory extraction
  outbox. It reuses the authoritative memory package instead of defining a
  second memory stack.
- `answer_readiness` owns typed question-dependency classification,
  the bounded readiness tool, and deterministic answer gating. It is reusable
  internal code and does not own an HTTP or Conversation entrypoint.
- `data_sync` owns desired-state CDC tasks, DW schema/backfill/event
  application, source adapters, and its dedicated process.
  `data_sync.application.service.DataSyncService.dispatch_once()` is the deep
  application interface. `application.contracts` owns technology-neutral task,
  source, materialization, and lease interfaces; it does not import SQLAlchemy,
  global settings, concrete repositories, source clients, schema synchronizers,
  or projection adapters. `data_sync.adapters` owns MySQL Sessions, repository
  construction, source engines, generation/schema locks, DW transactions, and
  production composition. `data_sync.worker` is the composition root that
  selects the concrete Meta Projection transaction participant.
- `models` owns Pydantic contracts shared across HTTP, workflow,
  persistence, Conversation, and long-term memory.
- `memory` owns deterministic memory rules, application use cases,
  authoritative MySQL persistence, and rebuildable index adapters.
- `persistence.schema.metadata` is the single SQLAlchemy `MetaData`
  shared by Conversation, Meta snapshot, and memory tables.
- `errors` and `identifiers` own stable cross-feature
  business errors and identifiers.
- `infrastructure` owns one explicit external-resource lifecycle per
  module. It does not own feature orchestration.
- `ddl_metadata.api` owns HTTP request/response mapping. Job and memory routes
  are separate owners, `api.job_events` owns SSE framing/generation, and
  `api.router` aggregates the routes.
- `ddl_metadata.jobs.store.DDLJobStore` is the application-facing job facade.
  Redis keys, codecs, Lua scripts, state, events, outboxes, and leases are
  private technology-specific collaborators under `jobs.redis`.
- `memory.domain` contains deterministic transformations only. It
  must not import FastAPI, arq, initialized clients, SQLAlchemy sessions, Redis,
  Elasticsearch, Qdrant, or TEI.
- `memory.application` owns memory use cases and explicit ports. It depends on
  domain values and stable contracts only; it does not import MySQL repositories,
  SQLAlchemy, infrastructure clients, external SDKs, or global settings.
- `memory.adapters` implements the MySQL, search-index, projection-index, and
  composition roles. MySQL adapters own short use-case transactions; the API and
  worker composition roots inject initialized external clients and explicit
  budgets into long-lived application instances.
- `memory.mysql.MemoryRepository` owns authoritative records,
  history, links, browser mutations, and exact reads.
  `MemoryIndexOutboxRepository` separately owns desired index state, claims,
  retries, acknowledgements, projections, and rebuild scans. Both receive the
  caller's `AsyncSession` so record-plus-outbox writes remain atomic.
- `memory.indexing` owns the concrete Elasticsearch/Qdrant implementations and
  rebuild orchestration for derived projections. Dispatch orchestration belongs
  to `memory.application.index_dispatcher`; outer adapters inject the concrete
  projection targets.
- `ddl_metadata.persistence` owns the Meta snapshot tables/repository. Its
  memory-reference adapter validates DDL-specific table, column, and metric
  references for root memory use cases.
- `ddl_metadata.application.accepted_snapshot` owns the immutable accepted
  snapshot command and publication interface. The
  `ddl_metadata.adapters.mysql.accepted_snapshot` integration adapter owns the
  generation-lock-protected single transaction that atomically composes Meta,
  root memory, Data Sync desired state, and Meta Projection outbox work.
- `ddl_metadata.meta_projection` owns the rebuildable semantic and value search
  representation of an accepted Meta Snapshot. Its domain/application modules
  do not import SQLAlchemy, global settings, external SDKs, or Data Sync
  persistence implementations; adapters own MySQL and remote-index details.
- `ddl_metadata.workflow.state` and `.contracts` are importable without graph
  construction; `.nodes` owns dependency-bound node behavior, `.routing` owns
  pure conditional routing, and `.graph` only registers and compiles topology.
- `ddl_metadata.worker.job_runner` owns one DDL execution/recovery unit,
  `.maintenance` owns scheduled jobs, `.lifecycle` owns resources and graph
  composition, and `.settings.WorkerSettings` is the only arq discovery class.
- Models, parsing, validation, identifiers, errors, settings, `app_logging`, and root
  composition remain cohesive. File size alone is not a reason to split them.

## Dependency Direction

```text
main/application
  -> conversation + ddl_metadata/api/router + memory/application
  -> infrastructure lifecycle

future answer caller
  -> answer_readiness -> data_sync/repository + infrastructure/llm_client

data_sync/worker
  -> data_sync/adapters/composition + source clients
  -> ddl_metadata/meta_projection/adapters (composition only)

data_sync/application
  -> data_sync/models + application/contracts

data_sync/adapters
  -> data_sync/application + repository/backfill/schema_sync/binlog
  -> infrastructure/mysql + Meta Projection application input

worker/settings
  -> worker/job_runner + worker/maintenance + worker/lifecycle

worker/job_runner
  -> jobs/store + workflow state

workflow/graph
  -> workflow nodes/routing/contracts/state/memory_context

workflow/nodes
  -> memory/domain + parsing + validation + models

memory/application
  -> memory/domain + models

memory/adapters
  -> memory/application + memory/mysql + memory/indexing + infrastructure

conversation/application
  -> conversation/models + memory/domain values + models

conversation/adapters
  -> conversation/application + conversation/repository + memory/application
     + memory/mysql + infrastructure

jobs/store
  -> jobs/redis
```

Conversation depends on root `models`, `memory`, `persistence`, `errors`, and
`identifiers`; it must not import `ddl_metadata`. Technology-specific
packages may depend on typed models, settings, and shared
infrastructure clients, but deterministic domain modules never depend on
technology-specific packages. API, worker, workflow, and memory consumers use
the application-facing job facade and do not import `jobs.redis`.

## Naming and Package Rules

- Python packages, modules, functions, variables, and configuration keys use
  `snake_case`; classes use `PascalCase`.
- Established acronyms retain `DDL`, `LLM`, `API`, `TEI`, and `MySQL`.
- Stateful persistence classes use precise suffixes such as `Repository` or
  `Store`. Pure Redis primitives are `JobKeys`, `JobCodec`, and `JobScripts`.
- Public runtime and test objects use Chinese Google Style Docstrings.
- Every package `__init__.py` contains only a meaningful package Docstring and
  has no imports, initialized clients, configuration mutation, or side effects.
- Consumers import from the concrete owning module. Retired internal paths do
  not receive compatibility shims.
- Do not introduce generic root `utils`, `common`, `manager`, `service`, or
  repository packages without a demonstrated cross-feature contract.

## Compatibility Boundaries

Package moves must preserve HTTP route metadata, Pydantic contracts, arq
function and cron names, Redis keys/hash fields/canonical JSON/Lua behavior,
MySQL schemas and statements, LangGraph state keys/node names/edge topology,
configuration keys/defaults, and structured logging event/field names. Package
refactors require a hard migration across source, tests, active documentation,
configuration, and current specs; archived Trellis artifacts remain historical.

## Scenario: Shared Memory Boundary

### 1. Scope / Trigger

- Trigger: Conversation and DDL metadata both consume long-term memory, so the
  memory use cases and contracts must have a root owner without reverse
  dependencies on either feature package.

### 2. Signatures

- `MemoryVersions(content: str, projection: str)` is an
  immutable domain input supplied by composition or workflow code.
- `MemoryMutationLeaseProvider` and `MemoryReferenceValidator` are application
  protocols. DDL-specific implementations are injected at the composition
  root.
- `persistence.schema.metadata` is the only shared SQLAlchemy
  `MetaData` instance used by Conversation, Meta snapshot, and memory tables.

### 3. Contracts

- `memory.application` may depend on root models, identifiers, errors,
  and memory domain values. Concrete persistence, settings, and infrastructure
  dependencies remain in `memory.adapters` or composition roots.
- `conversation.application` collaborates with Long-term Memory only
  through its application interface. The two intentional atomic cross-context
  transactions live in outer MySQL integration adapters for user-data erasure and
  extraction completion.
- `memory` and `conversation` must not import
  `ddl_metadata`.
- DDL-specific snapshot context and reference validation remain in
  `ddl_metadata.workflow` and `ddl_metadata.persistence`.
- Package moves are hard migrations; active code and current specs receive no
  compatibility modules for retired paths.

### 4. Validation & Error Matrix

- Missing injected lease/reference collaborator -> fail at composition or use
  case construction, not by importing a DDL implementation from memory.
- Invalid DDL table, column, or metric reference -> DDL reference adapter
  rejects the mutation through the existing business-error contract.
- A second SQLAlchemy `MetaData` instance -> schema identity test fails.
- A retired shared-package import -> repository path search fails.

### 5. Good/Base/Bad Cases

- Good: DDL workflow creates `MemoryVersions` explicitly and injects DDL
  adapters into the root memory service.
- Base: Conversation uses root memory services and models without knowing DDL
  metadata exists.
- Bad: `memory.domain` reads `app_config`, or root memory imports a concrete
  class from `ddl_metadata`.

### 6. Tests Required

- Unit test that changing versions preserves memory UID and content hash while
  retaining the supplied version values.
- Unit test that Conversation, Meta snapshot, and memory table modules expose
  the identical root `MetaData` object.
- Repository search asserting zero active retired imports and zero
  `conversation -> ddl_metadata` or `memory -> ddl_metadata` imports.
- Ruff, Pyright, compileall, settings loading, and non-integration pytest must
  pass after every boundary migration.

### 7. Wrong vs Correct

#### Wrong

```python
from ddl_metadata.persistence.memory_references import (
    MetadataMemoryReferenceValidator,
)

versions = MemoryVersions(
    content=app_config.memory.content_version,
    projection=app_config.memory.projection_version,
)
```

#### Correct

```python
service = MemoryService(
    leases=ddl_job_store,
    references=MetadataMemoryReferenceValidator(),
)

versions = MemoryVersions(
    content=settings.memory.content_version,
    projection=settings.memory.projection_version,
)
```
