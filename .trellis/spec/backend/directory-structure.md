# Backend Directory Structure

## Current Scope

This repository is a backend-only installable Python application. Runtime code
lives under `src/data_agent/` and is organized by business feature first.
Shared Pydantic contracts live under `models`, long-term memory is a root
business module used by both DDL metadata and Conversation, and shared
external-resource lifecycles live under `infrastructure`. DDL metadata retains
only its transport, job orchestration, Meta snapshot persistence, workflow,
and worker entry points.

## Directory Layout

```text
src/data_agent/
├── application.py
├── logging.py
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
│   ├── application/
│   │   ├── search.py
│   │   └── service.py
│   ├── domain/
│   │   ├── candidates.py
│   │   ├── lifecycle.py
│   │   ├── payloads.py
│   │   ├── policies.py
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
│   ├── extraction.py
│   ├── models.py
│   ├── mysql_tables.py
│   ├── repository.py
│   └── service.py
├── answer_readiness/
│   ├── classifier.py
│   ├── models.py
│   ├── service.py
│   └── tool.py
├── data_sync/
│   ├── backfill.py
│   ├── binlog.py
│   ├── models.py
│   ├── repository.py
│   ├── schema_sync.py
│   ├── service.py
│   ├── tables.py
│   └── worker.py
└── ddl_metadata/
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
    │   ├── snapshots.py
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

Tests mirror deterministic package boundaries under `tests/unit/`; integration
tests remain scenario-oriented under `tests/integration/`. Configuration stays
under `conf/`, while local service definitions and SQL bootstrap assets stay
under `docs/docker/`.

## Ownership

- `data_agent.application` is the FastAPI composition root and lifecycle owner.
- `data_agent.conversation` owns permanent text conversations, turn
  idempotency, bounded context, and the leased conversation-memory extraction
  outbox. It reuses the authoritative memory package instead of defining a
  second memory stack.
- `data_agent.answer_readiness` owns typed question-dependency classification,
  the bounded readiness tool, and deterministic answer gating. It is reusable
  internal code and does not own an HTTP or Conversation entrypoint.
- `data_agent.data_sync` owns desired-state CDC tasks, DW schema/backfill/event
  application, source adapters, and its dedicated process.
- `data_agent.models` owns Pydantic contracts shared across HTTP, workflow,
  persistence, Conversation, and long-term memory.
- `data_agent.memory` owns deterministic memory rules, application use cases,
  authoritative MySQL persistence, and rebuildable index adapters.
- `data_agent.persistence.schema.metadata` is the single SQLAlchemy `MetaData`
  shared by Conversation, Meta snapshot, and memory tables.
- `data_agent.errors` and `data_agent.identifiers` own stable cross-feature
  business errors and identifiers.
- `data_agent.infrastructure` owns one explicit external-resource lifecycle per
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
- `memory.application` owns memory use cases and transaction boundaries. It
  composes domain behavior, MySQL repositories, index adapters, and injected
  DDL lease/reference interfaces without importing the DDL package or defining
  SQL and external payload formats.
- `memory.mysql.MemoryRepository` owns authoritative records,
  history, links, browser mutations, and exact reads.
  `MemoryIndexOutboxRepository` separately owns desired index state, claims,
  retries, acknowledgements, projections, and rebuild scans. Both receive the
  caller's `AsyncSession` so record-plus-outbox writes remain atomic.
- `memory.indexing` owns Elasticsearch/Qdrant adapters and the
  dispatcher/rebuilder use cases for derived projections.
- `ddl_metadata.persistence` owns the Meta snapshot tables/repository and the
  accepted-snapshot transaction that composes Meta with root memory persistence.
  Its memory-reference adapter validates DDL-specific table, column, and metric
  references for root memory use cases.
- `ddl_metadata.workflow.state` and `.contracts` are importable without graph
  construction; `.nodes` owns dependency-bound node behavior, `.routing` owns
  pure conditional routing, and `.graph` only registers and compiles topology.
- `ddl_metadata.worker.job_runner` owns one DDL execution/recovery unit,
  `.maintenance` owns scheduled jobs, `.lifecycle` owns resources and graph
  composition, and `.settings.WorkerSettings` is the only arq discovery class.
- Models, parsing, validation, identifiers, errors, settings, logging, and root
  composition remain cohesive. File size alone is not a reason to split them.

## Dependency Direction

```text
main/application
  -> conversation + ddl_metadata/api/router + memory/application
  -> infrastructure lifecycle

future answer caller
  -> answer_readiness -> data_sync/repository + infrastructure/llm_client

worker/settings
  -> worker/job_runner + worker/maintenance + worker/lifecycle

worker/job_runner
  -> jobs/store + workflow state

workflow/graph
  -> workflow nodes/routing/contracts/state/memory_context

workflow/nodes
  -> memory/domain + parsing + validation + models

memory/application
  -> memory/domain + memory/mysql + memory/indexing

jobs/store
  -> jobs/redis
```

Conversation depends on root `models`, `memory`, `persistence`, `errors`, and
`identifiers`; it must not import `data_agent.ddl_metadata`. Technology-specific
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
- `data_agent.persistence.schema.metadata` is the only shared SQLAlchemy
  `MetaData` instance used by Conversation, Meta snapshot, and memory tables.

### 3. Contracts

- `data_agent.memory` may depend on root models, identifiers, errors,
  persistence, settings outside the domain layer, and infrastructure adapters.
- `data_agent.memory` and `data_agent.conversation` must not import
  `data_agent.ddl_metadata`.
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
from data_agent.ddl_metadata.persistence.memory_references import (
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
