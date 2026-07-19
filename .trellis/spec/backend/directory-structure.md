# Backend Directory Structure

## Current Scope

This repository is a backend-only installable Python application. Runtime code
lives under `src/data_agent/` and is organized by business feature first.
Shared external-resource lifecycles live under `infrastructure`; DDL metadata
then separates transport, application orchestration, deterministic domain
logic, technology-specific persistence/indexing, workflow composition, and
worker entry points.

## Directory Layout

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

Tests mirror deterministic package boundaries under `tests/unit/`; integration
tests remain scenario-oriented under `tests/integration/`. Configuration stays
under `conf/`, while local service definitions and SQL bootstrap assets stay
under `docs/docker/`.

## Ownership

- `data_agent.application` is the FastAPI composition root and lifecycle owner.
- `data_agent.infrastructure` owns one explicit external-resource lifecycle per
  module. It does not own feature orchestration.
- `ddl_metadata.api` owns HTTP request/response mapping. Job and memory routes
  are separate owners and `api.router` aggregates them.
- `ddl_metadata.jobs.store.DDLJobStore` is the application-facing job facade.
  Redis keys, codecs, Lua scripts, state, outboxes, and leases are private
  technology-specific collaborators under `jobs.redis`.
- `ddl_metadata.memory.domain` contains deterministic transformations only. It
  must not import FastAPI, arq, initialized clients, SQLAlchemy sessions, Redis,
  Elasticsearch, Qdrant, or TEI.
- `ddl_metadata.memory.application` owns memory use cases and transaction
  boundaries. It composes domain behavior, MySQL repositories, index adapters,
  and Meta persistence without defining SQL or external payload formats.
- `ddl_metadata.memory.mysql.MemoryRepository` owns authoritative records,
  history, links, browser mutations, and exact reads.
  `MemoryIndexOutboxRepository` separately owns desired index state, claims,
  retries, acknowledgements, projections, and rebuild scans. Both receive the
  caller's `AsyncSession` so record-plus-outbox writes remain atomic.
- `ddl_metadata.memory.indexing` owns Elasticsearch/Qdrant adapters and the
  dispatcher/rebuilder use cases for derived projections.
- `ddl_metadata.persistence` owns the Meta snapshot tables/repository.
  `persistence.schema.metadata` is the single SQLAlchemy `MetaData` shared by
  Meta and memory table modules.
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
  -> api/router + jobs/store + memory/application
  -> infrastructure lifecycle

worker/settings
  -> worker/job_runner + worker/maintenance + worker/lifecycle

worker/job_runner
  -> jobs/store + workflow state

workflow/graph
  -> workflow nodes/routing/contracts/state

workflow/nodes
  -> memory/domain + parsing + validation + models

memory/application
  -> memory/domain + memory/mysql + memory/indexing + persistence metadata

jobs/store
  -> jobs/redis
```

Technology-specific packages may depend on typed models, settings, and shared
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
