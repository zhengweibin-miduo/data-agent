# Backend Directory Structure

## Current Scope

This repository is a backend-only installable Python application. Runtime code
lives under `src/data_agent/` and is organized by business feature first.
Shared external-resource lifecycles live under `infrastructure`; DDL metadata
contracts, workflow, persistence, API routes, and worker behavior live together
under `ddl_metadata`.

Keep application composition, infrastructure lifecycle, deterministic business
logic, persistence, and HTTP/worker boundaries separate. Do not move workflow
or SQL behavior into routes.

## Directory Layout

```text
src/data_agent/
├── __init__.py
├── main.py                         # ASGI object and executable entry
├── application.py                  # FastAPI factory and lifespan composition
├── settings.py                     # Typed YAML settings and shared instance
├── logging.py                      # Central Loguru sink configuration
├── infrastructure/
│   ├── __init__.py
│   ├── mysql.py                    # Engine, Session, transaction lifecycle
│   ├── redis.py                    # Decoded application Redis client
│   ├── checkpoint_store.py         # LangGraph Redis saver lifecycle
│   ├── llm_client.py               # OpenAI-compatible ChatOpenAI lifecycle
│   ├── tei_embeddings.py           # TEI/Hugging Face embedding lifecycle
│   ├── elasticsearch.py
│   └── qdrant.py
└── ddl_metadata/
    ├── __init__.py
    ├── api.py                      # Feature router and safe HTTP mapping
    ├── worker.py                   # arq execution, retry, and recovery
    ├── errors.py
    ├── identifiers.py
    ├── parsing.py
    ├── validation.py
    ├── models/
    │   ├── __init__.py
    │   ├── base.py
    │   ├── physical.py
    │   ├── semantic.py
    │   ├── memory.py
    │   └── jobs.py
    ├── workflow/
    │   ├── __init__.py
    │   ├── graph.py
    │   └── metadata_generator.py
    ├── jobs/
    │   ├── __init__.py
    │   └── store.py
    ├── memory/
    │   ├── __init__.py
    │   ├── context.py
    │   ├── payloads.py
    │   ├── service.py
    │   └── snapshots.py
    └── persistence/
        ├── __init__.py
        ├── tables.py
        ├── metadata_repository.py
        └── memory_repository.py
tests/
├── conftest.py
├── helpers/
├── unit/
│   ├── infrastructure/
│   └── ddl_metadata/
└── integration/
    ├── infrastructure/
    ├── persistence/
    ├── test_api.py
    ├── test_worker.py
    ├── test_memory_services.py
    └── test_ddl_metadata_flow.py
conf/
└── app_config.yaml
docs/docker/
├── docker-compose.yml
├── elasticsearch/
└── mysql/
```

The distribution name is `data-agent`; its Python import package is
`data_agent`. The project uses a declared build backend and `uv sync` installs
the package from `src/`.

## Module Boundaries

- `data_agent.settings` owns Pydantic settings schemas, YAML loading, and the
  shared `app_config` instance.
- `data_agent.infrastructure` owns explicit third-party resource lifecycle.
  Resource wrappers keep idempotent `initialize()`, guarded `get_client()`, and
  async `close()` behavior where applicable.
- `data_agent.ddl_metadata.models` owns typed contracts shared by HTTP,
  workflow state, Redis projections, model responses, and persistence.
- `data_agent.application` owns the FastAPI factory, configured CORS, shared
  API resource lifespan, router inclusion, and application-level setup.
- `data_agent.ddl_metadata.api` owns DDL metadata routes and safe
  exception-to-HTTP mapping; it does not mutate Redis or SQL directly.
- `data_agent.ddl_metadata.workflow` owns LangGraph orchestration and the typed
  metadata-generation boundary.
- `data_agent.ddl_metadata.jobs` owns revision-aware Redis job transitions,
  leases, dispatch, retention, and cleanup outboxes.
- `data_agent.ddl_metadata.memory` owns trusted-memory context loading,
  payload construction/rebuilding, snapshot persistence orchestration, and
  browser-facing management behavior.
- `data_agent.ddl_metadata.persistence` owns SQLAlchemy Core tables and bound
  statements. Repositories receive `AsyncSession`; transaction ownership stays
  with `MySQLDatabase.session()` and the calling service.
- `data_agent.ddl_metadata.worker` owns arq activation, checkpoint
  reconciliation, bounded retry, outbox dispatch, and waiting-input expiry.
- `data_agent.logging` owns Loguru sink setup. Application and worker
  lifecycles call it before emitting application logs.
- `tests/unit` contains deterministic tests without live services.
  `tests/integration` contains tests requiring MySQL, Redis, optional TEI, or
  combined application boundaries. TEI tests carry the additional `tei`
  marker so CI can exclude them unless the service is provisioned. Reusable
  fakes and factories live in `tests/helpers`, never another `test_*.py`
  module.
- `conf/` contains configuration values; `data_agent.settings` contains Python
  schemas and loading behavior.
- `docs/docker/` owns local infrastructure, not application runtime code.

There is no generic root `utils`, `common`, `service`, or `repository` package
and no ORM entity layer. Keep deterministic helpers beside their feature owner
until a real cross-feature abstraction exists.

## Naming Conventions

- Python packages, modules, functions, variables, and configuration keys use
  `snake_case`.
- Classes use `PascalCase`; established acronyms keep their canonical forms,
  including `DDL`, `LLM`, `API`, `TEI`, and `MySQL`.
- Runtime configuration classes use the `Settings` suffix.
- Capability names use precise suffixes such as `Client`, `Repository`,
  `Store`, `Service`, `Loader`, or `Factory`. Do not introduce generic
  `Manager`, `Helper`, or `Utils` names when a concrete responsibility exists.
- Test modules use a `test_` prefix and pytest collects them from `tests/`.
- Public packages, modules, classes, functions, methods, fixtures, and tests
  use Chinese Google Style Docstrings.
- Package `__init__.py` files contain a meaningful package Docstring, import no
  application modules, and perform no connection or configuration side
  effects.

## Dependency Direction

The intended direction is:

```text
main/application
    ├── infrastructure
    └── ddl_metadata.api / ddl_metadata.worker
            ├── workflow / jobs / memory
            │       ├── models
            │       └── persistence
            └── models
```

Models, parsing, validation, identifiers, and errors do not depend on FastAPI,
arq, initialized Redis clients, or active SQLAlchemy Sessions. Persistence does
not own commits or resource lifecycle.

## External-Service Layout

A configured shared dependency normally has:

1. A typed section in `data_agent.settings` and matching values in
   `conf/app_config.yaml`.
2. A lifecycle adapter under `data_agent.infrastructure`.
3. A local service definition under `docs/docker/` when it runs locally.
4. Focused unit or marked integration coverage under `tests/`.

Redis uses separate application-client and LangGraph-checkpoint wrappers because
their serialization and lifecycle contracts differ. The LLM client reads its
API key only from `DATA_AGENT_LLM_API_KEY`; YAML contains non-secret model
settings only.

## Retired Layout

The hard migration does not provide compatibility packages for:

- `app.*`;
- `app_test.*`;
- repository-root `main.py`;
- horizontal root `api`, `client`, `model`, `service`, `repository`, or
  `worker` ownership.

Active source, CI, current specs, and validation commands must use
`data_agent.*` and `tests/`. Archived tasks and developer journals remain
historical records and may retain paths that were correct when written.
