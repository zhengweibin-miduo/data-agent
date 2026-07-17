# Technical design

## 1. Architecture

The repository becomes one installable Python application with an import root
at `src/data_agent`. Runtime ownership is organized by business feature first,
with shared external-resource lifecycles isolated under `infrastructure`.

```text
src/data_agent/
├── __init__.py
├── main.py                    # ASGI object and executable application entry
├── application.py             # FastAPI factory and application lifespan
├── settings.py                # Pydantic settings schema and YAML loading
├── logging.py                 # Loguru configuration
├── infrastructure/
│   ├── __init__.py
│   ├── mysql.py
│   ├── redis.py
│   ├── checkpoint_store.py
│   ├── llm_client.py
│   ├── tei_embeddings.py
│   ├── elasticsearch.py
│   └── qdrant.py
└── ddl_metadata/
    ├── __init__.py
    ├── api.py                 # Feature router and HTTP mapping
    ├── worker.py              # arq settings and recovery
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
```

Directories are created only when the listed responsibility has real code.
There is no `utils`, `common`, generic `service`, or generic `repository`
package at the import root.

## 2. Dependency boundaries

Expected dependency direction:

```text
application/main
    ├── infrastructure
    └── ddl_metadata.api / ddl_metadata.worker
            ├── ddl_metadata.workflow / jobs / memory
            │       ├── ddl_metadata.models
            │       └── ddl_metadata.persistence
            └── ddl_metadata.models

settings/logging <- application and infrastructure
```

- `models`, deterministic parsing, validation, identifiers, and errors do not
  import FastAPI, arq, Redis clients, or SQLAlchemy sessions.
- `persistence` owns SQLAlchemy Core table declarations and bound statements.
- `infrastructure.mysql.MySQLDatabase` owns engine/session transaction
  lifecycle; repositories continue to receive `AsyncSession`.
- `application.py` is the composition root. It creates the FastAPI application,
  owns shared API resource lifespan, and includes the DDL metadata router.
- `ddl_metadata.worker` remains a separate arq composition boundary.
- `__init__.py` files are documented and side-effect free. They do not eagerly
  initialize configuration or external resources.

## 3. Packaging and entry points

- Distribution name remains `data-agent`; import package becomes `data_agent`.
- Add a uv-compatible build backend and package configuration for `src/`.
- `uv sync --locked` installs the application in editable mode for development.
- `data_agent.main:app` is the ASGI target.
- `python -m data_agent.main` remains a supported local executable entry.
- The old root `main.py` and old `app` package are removed without forwarding
  imports.

## 4. Public identifier migration

### Infrastructure and settings

| Old | New |
|---|---|
| `ConfigModel` | `SettingsModel` |
| `LogFileConfig` | `FileLoggingSettings` |
| `LogConsoleConfig` | `ConsoleLoggingSettings` |
| `LoggingConfig` | `LoggingSettings` |
| `QdrantConfig` | `QdrantSettings` |
| `ElasticsearchConfig` | `ElasticsearchSettings` |
| `TeiConfig` | `TEISettings` |
| `MysqlConfig` | `MySQLSettings` |
| `ApiConfig` | `APISettings` |
| `RedisConfig` | `RedisSettings` |
| `LlmConfig` | `LLMSettings` |
| `MemoryConfig` | `MemorySettings` |
| `AppConfigModel` | `AppSettings` |
| `MysqlClientManager` | `MySQLDatabase` |
| `RedisClientManager` | `RedisClient` |
| `CheckpointClientManager` | `CheckpointStore` |
| `LlmClientManager` | `LLMClient` |
| `TeiEmbeddings` | `TEIEmbeddings` |
| `TeiEmbeddingClientManager` | `TEIEmbeddingClient` |
| `ElasticsearchClientManager` | `ElasticsearchClient` |
| `QdrantClientManager` | `QdrantClient` |

The renamed lifecycle wrappers preserve their current initialize/get/close
semantics. This task does not replace them with a dependency-injection
framework.

### DDL metadata

All public `Ddl*` and `Llm*` class names become `DDL*` and `LLM*`.
Important responsibility refinements:

| Old | New |
|---|---|
| `DdlMetadataError` | `DDLMetadataError` |
| `DdlGraphState` | `DDLGraphState` |
| `DdlGraphDependencies` | `DDLGraphDependencies` |
| `DdlJobRequest` | `DDLJobRequest` |
| `DdlJobAccepted` | `DDLJobAccepted` |
| `LlmMetadataModel` | `LLMMetadataGenerator` |
| `MetadataModel` protocol | `MetadataGenerator` |
| `MetaRepository` | `MetadataRepository` |
| `JobStore` | `DDLJobStore` |
| `SnapshotService` | `MetadataSnapshotService` |
| `MemoryManagementService` | `MemoryService` |
| `MemoryContextService` | `MemoryContextLoader` |

Names already precise, such as `MemoryRepository`, `MemoryPayloadRebuilder`,
`PhysicalSchema`, and `SemanticMetadata`, remain unchanged.

## 5. Models and OpenAPI

- The current monolithic model module is split by contract ownership.
- Pydantic field names, aliases, validation behavior, serialization behavior,
  enum values, and `extra="forbid"` remain unchanged.
- FastAPI OpenAPI component names may change with Python class names.
- A before/after compatibility check compares paths, operations, parameters,
  request/response fields, required fields, status codes, and enum values while
  allowing component-key renames.

## 6. Tests

```text
tests/
├── conftest.py
├── helpers/
│   ├── __init__.py
│   ├── factories.py
│   └── fakes.py
├── unit/
│   ├── infrastructure/
│   └── ddl_metadata/
└── integration/
    ├── persistence/
    ├── test_api.py
    ├── test_worker.py
    └── test_ddl_metadata_flow.py
```

- pytest collects all `test_*` functions.
- Async tests use `pytest.mark.asyncio` or configured automatic asyncio mode;
  synchronous wrappers and module main guards are deleted.
- Tests requiring live MySQL or Redis carry an `integration` marker.
- Fake model implementations and reusable data builders move out of
  `test_*.py` modules.
- Test modules and public test functions receive concise Chinese Docstrings so
  the same public-object rule applies uniformly.
- CI starts MySQL/Redis as it does today and runs the complete suite. Focused
  local commands can exclude integration tests with `-m "not integration"`.

## 7. Docstring and comment contract

- PEP 257 structure plus Google Style sections.
- Chinese prose; identifiers, protocol names, and technical product terms stay
  in English.
- Public package/module/class/function/method coverage is enforced.
- Simple objects use one-line Docstrings. Complex objects document applicable
  arguments, return/yield value, exceptions, side effects, concurrency,
  transactions, and lifecycle constraints.
- Type information is not duplicated from annotations.
- Inline comments explain rationale or invariants. `TODO` includes an owner or
  issue and a concrete removal/completion condition.
- Commented-out code and historical change narration are removed.

Ruff enables the `D` rules with Google convention. Chinese prose may ignore
English-only imperative mood and terminal punctuation rules (`D400`, `D401`,
`D415`), but missing public package/module/class/function/method checks remain
active.

## 8. Tooling and CI

- Persist Ruff, Pyright, pytest, and pytest async support in a development
  dependency group instead of relying on temporary `uv --with` resolution.
- Configure pytest paths, import mode, asyncio mode, and the `integration`
  marker in `pyproject.toml`.
- Ruff and Pyright run against `src` and `tests`.
- compileall runs against `src` and `tests`.
- Configuration validation imports `data_agent.settings`.
- Tests run through pytest, not `python -m` per test module.
- `uv.lock` is regenerated and checked.

## 9. Specs and documentation

Update all current `.trellis/spec/backend/` guides whose live contracts mention:

- `app/`, `app_test/`, root `main.py`, or old module paths;
- `*ClientManager`, old settings class names, or old DDL/LLM acronym casing;
- per-module test commands;
- absence of persistent Ruff/Pyright/pytest configuration.

Do not rewrite archived tasks or journals. The backend spec index continues to
require English project specification prose even though runtime Docstrings are
Chinese.

## 10. Migration and rollback

The implementation is an atomic repository migration:

1. Capture the clean baseline and behavior/schema evidence.
2. Add packaging/tool configuration.
3. Move and rename runtime modules in dependency order.
4. Move and convert tests.
5. Enable strict Docstring checks after coverage is complete.
6. Update CI and current Trellis specs.
7. Run the full quality and integration gate.

No database or Redis migration is required because persisted keys, table names,
columns, enum values, and serialization fields do not change.

Before commit, rollback is a normal source-control revert of this task's
changes. Do not use destructive Git history commands. If an intermediate move
breaks imports, continue the atomic migration rather than adding temporary
compatibility packages.

