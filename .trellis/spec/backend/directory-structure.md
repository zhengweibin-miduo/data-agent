# Backend Directory Structure

## Current Scope

This repository is a backend-only Python application. Runtime code is under
`app/` and now includes a loopback HTTP API, deterministic/LLM orchestration,
repository-owned persistence, and an asynchronous worker. Keep those
responsibilities separate instead of moving workflow or SQL logic into routes.

## Directory Layout

```text
app/
├── __init__.py
├── api/
│   ├── __init__.py
│   └── app.py             # FastAPI factory, lifespan, routes, error mapping
├── client/                 # Async external-service client managers
│   ├── __init__.py
│   └── *_client_manager.py
├── model/
│   ├── __init__.py
│   └── ddl_metadata.py    # Shared Pydantic boundary contracts
├── repository/
│   ├── __init__.py
│   └── ddl_metadata/
│       ├── __init__.py
│       ├── schema.py      # Meta tables + schema-qualified memory tables
│       ├── meta.py
│       └── memory.py
├── service/
│   ├── __init__.py
│   └── ddl_metadata/
│       ├── __init__.py
│       ├── graph.py
│       ├── parser.py
│       ├── validator.py
│       ├── llm.py
│       ├── job_store.py
│       ├── memory.py
│       ├── memory_context.py
│       ├── memory_management.py
│       ├── identifiers.py
│       └── errors.py
├── worker/
│   ├── __init__.py
│   └── ddl_metadata.py
├── core/
│   ├── __init__.py
│   └── logging.py          # Central Loguru sink configuration
└── conf/
    ├── __init__.py
    └── app_config.py       # Typed configuration models and shared config
app_test/
├── __init__.py
├── api/
├── client/
├── core/
├── integration/
├── repository/ddl_metadata/
├── service/ddl_metadata/
└── worker/                 # Mirrors the runtime package boundaries
conf/
└── app_config.yaml         # Local application configuration values
docs/docker/
├── docker-compose.yml      # Local service definitions
└── elasticsearch/          # Service-specific image customization
main.py                     # Uvicorn entry point using the application factory
```

## Module Boundaries

- `app/conf/app_config.py` owns the Pydantic configuration schema, YAML loading,
  and the shared `app_config` instance.
- `app/client/` owns lifecycle adapters around third-party async clients. The
  repeated local shape is `initialize()`, `get_client()`, and async `close()`;
  `CheckpointClientManager.initialize()` is async because it also owns
  checkpointer context entry and `asetup()`.
- `app/model/ddl_metadata.py` is the shared typed contract owner for HTTP,
  graph state values, Redis projections, model responses, and repositories.
- `app/api/app.py` owns the FastAPI factory, configured CORS, lifespan-managed
  API clients, route serialization, and safe exception-to-HTTP mapping.
  `ApiConfig` fixes the bind host to `127.0.0.1` and rejects any CORS Origin
  whose host is not `localhost` or a loopback IP.
- `app/service/ddl_metadata/` owns parsing, deterministic validation and
  identifiers, LangGraph orchestration, Redis job transitions, model
  adaptation, and memory application behavior. Services do not own SQL text
  or FastAPI request types.
- `app/repository/ddl_metadata/` owns SQLAlchemy Core tables and bound
  statements. Repository instances receive an `AsyncSession`; transaction
  commit/rollback remains with `MysqlClientManager.session()` and the calling
  service.
- `app/worker/ddl_metadata.py` owns arq activation, graph checkpoint
  reconciliation, bounded retry, outbox dispatch, and waiting-input expiry.
- `app/core/logging.py` owns Loguru sink setup; API and worker lifecycles call
  it before application logs.
- `app_test/` mirrors runtime boundaries and the DDL metadata service/repository
  domain packages while keeping focused executable modules.
  `app_test/integration/test_ddl_metadata_flow.py` is the combined live
  Redis-checkpoint/MySQL flow.
- `conf/` contains values, while `app/conf/` contains Python models and loading
  behavior. Keep this distinction when adding a configuration field.
- `docs/docker/` is local infrastructure, not application runtime code. Keep
  service image and volume details there.

There is no generic `utils` package or ORM entity layer. Keep deterministic
helpers beside their owner (`identifiers.py`, parser, validator) until a real
cross-feature abstraction exists.

## Naming Conventions

- Python packages, modules, functions, and configuration keys use
  `snake_case`.
- Client lifecycle modules use the singular package `app/client/` and the file
  suffix `_client_manager.py`.
- Manager and configuration classes use `PascalCase`, for example
  `MysqlClientManager`, `CheckpointClientManager`, and `MysqlConfig`.
- Test modules mirror the runtime module name with a `test_` prefix.
- Package directories contain `__init__.py`; the current marker files contain
  only package docstrings. They import no application modules and perform no
  connection or configuration side effects.

## Observed External-Service Layout

Configured infrastructure follows these repository-backed paths:

1. A typed section in `app/conf/app_config.py` and a matching key in
   `conf/app_config.yaml`.
2. An async lifecycle adapter under `app/client/`.
3. A local service definition under `docs/docker/` when the dependency runs
   locally.
4. A focused executable manager check under `app_test/client/` when application
   code owns lifecycle or compatibility behavior.

Redis uses separate managers for the decoded application client and the
LangGraph checkpointer because their lifecycle and serialization requirements
differ. The LLM manager reads its API key only from
`DATA_AGENT_LLM_API_KEY`; YAML owns only non-secret model settings.

Use `external-service-integrations.md` for TEI, MySQL, Redis/checkpoint, and LLM
contracts; use `database-guidelines.md` for repository transactions and
snapshot scope; use `logging-guidelines.md` for Loguru behavior.
