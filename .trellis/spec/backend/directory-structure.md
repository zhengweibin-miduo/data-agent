# Backend Directory Structure

## Current Scope

This repository is currently a small, backend-only Python application. Runtime
code is under `app/`; there is no HTTP route layer, business-service layer, or
worker package yet. Do not invent those boundaries when making a client or
configuration change.

## Directory Layout

```text
app/
├── __init__.py
├── client/                 # Async external-service client managers
│   ├── __init__.py
│   └── *_client_manager.py
├── core/
│   ├── __init__.py
│   └── logging.py          # Central Loguru sink configuration
└── conf/
    ├── __init__.py
    └── app_config.py       # Typed configuration models and shared config
app_test/
├── __init__.py
├── client/                 # Manager checks and live integrations
│   ├── __init__.py
│   └── test_*_client_manager.py
└── core/
    ├── __init__.py
    └── test_logging.py     # Isolated logging configuration check
conf/
└── app_config.yaml         # Local application configuration values
docs/docker/
├── docker-compose.yml      # Local service definitions
└── elasticsearch/          # Service-specific image customization
main.py                     # Entry point that configures logging
```

## Module Boundaries

- `app/conf/app_config.py` owns the Pydantic configuration schema, YAML loading,
  and the shared `app_config` instance.
- `app/client/` owns lifecycle adapters around third-party async clients. The
  repeated local shape is `initialize()`, `get_client()`, and async `close()`;
  see `mysql_client_manager.py`, `qdrant_client_manager.py`, and
  `elasticsearch_client_manager.py`.
- `app/core/logging.py` owns Loguru sink setup; `main.py` calls it before the
  first application log.
- `app_test/client/` mirrors `app/client/` for manager behavior and live
  integration checks. `app_test/core/` validates logging in isolation.
- `conf/` contains values, while `app/conf/` contains Python models and loading
  behavior. Keep this distinction when adding a configuration field.
- `docs/docker/` is local infrastructure, not application runtime code. Keep
  service image and volume details there.

There are currently no `routes`, `services`, `repositories`, `models`, or
generic `utils` packages. Add a new layer only when a feature establishes a
real responsibility for it, then update this guide with the resulting pattern.

## Naming Conventions

- Python packages, modules, functions, and configuration keys use
  `snake_case`.
- Client lifecycle modules use the singular package `app/client/` and the file
  suffix `_client_manager.py`.
- Manager and configuration classes use `PascalCase`, for example
  `MysqlClientManager`, `TeiEmbeddingClientManager`, and `MysqlConfig`.
- Test modules mirror the runtime module name with a `test_` prefix.
- Package directories contain `__init__.py`; the current marker files contain
  only package docstrings. They import no application modules and perform no
  connection or configuration side effects.

## Observed External-Service Layout

Every currently configured service has the following repository-backed pieces:

1. A typed section in `app/conf/app_config.py` and a matching key in
   `conf/app_config.yaml`.
2. An async lifecycle adapter under `app/client/`.
3. A local service definition under `docs/docker/`.

Executable live checks currently exist only for MySQL and TEI, under
`app_test/client/`. Qdrant and Elasticsearch currently have no executable check
in that package.

Use `.trellis/spec/backend/external-service-integrations.md` for the stricter
TEI and MySQL contracts, and `logging-guidelines.md` for Loguru behavior.
