# Backend Directory Structure

## Current Scope

This repository is a small, backend-only Python application. Runtime code is
under `app/`. Metadata synchronization establishes one MVC-like CLI path with a
Controller, Service, and Repository; there is still no HTTP route, View,
scheduler, or worker package. The flow now has concrete Meta MySQL Models and
business Entities; do not generalize them into framework layers for unrelated
client or configuration changes.

## Directory Layout

```text
app/
├── __init__.py
├── client/                 # Async external-service client managers
│   ├── __init__.py
│   └── *_client_manager.py
├── conf/
│   ├── __init__.py
│   ├── app_config.py       # Shared infrastructure configuration
│   └── meta_config.py      # Strict metadata YAML model
├── core/
│   ├── __init__.py
│   └── logging.py          # Central Loguru sink configuration
├── entity/                 # Metadata business dataclasses
│   ├── __init__.py
│   └── *.py
├── model/                  # SQLAlchemy mappings for meta.* tables
│   ├── __init__.py
│   ├── base.py
│   └── *.py
├── repository/
│   ├── __init__.py
│   └── metadata_repository.py  # DW reads and storage-specific upserts
├── script/
│   ├── __init__.py
│   └── sync_metadata.py    # CLI Controller and client lifecycle
└── service/
    ├── __init__.py
    └── metadata_sync_service.py # Validation, conversion, and orchestration
app_test/
├── __init__.py
├── client/                 # Manager checks and live integrations
│   ├── __init__.py
│   └── test_*_client_manager.py
├── core/
    ├── __init__.py
    └── test_logging.py     # Isolated logging configuration check
└── service/
    ├── __init__.py
    └── test_metadata_sync_service.py # Dependency-isolated sync checks
conf/
├── app_config.yaml         # Local infrastructure configuration values
└── meta_config.yaml        # Metadata tables, columns, and metrics
docs/docker/
├── docker-compose.yml      # Local service definitions
└── elasticsearch/          # Service-specific image customization
main.py                     # Entry point that configures logging
```

## Module Boundaries

- `app/conf/app_config.py` owns the Pydantic configuration schema, YAML loading,
  and the shared `app_config` instance.
- `app/conf/meta_config.py` owns the separate, explicitly loaded metadata YAML
  contract. It reuses `ConfigModel` but does not mutate the shared `app_config`.
- `app/client/` owns lifecycle adapters around third-party async clients. The
  repeated local shape is `initialize()`, `get_client()`, and async `close()`;
  see `mysql_client_manager.py`, `qdrant_client_manager.py`, and
  `elasticsearch_client_manager.py`.
- `app/core/logging.py` owns Loguru sink setup; `main.py` calls it before the
  first application log.
- `app/entity/` owns the standard parameters passed from Service to Repository:
  `TableInfo`, `ColumnInfo`, `MetricInfo`, `ColumnMetric`, and `ValueInfo`.
  `ValueInfo` is Elasticsearch-only and has no MySQL mapping.
- `app/model/` owns the SQLAlchemy mappings for the four existing `meta` tables.
  Models match `docs/docker/mysql/meta.sql` and do not add foreign keys,
  relationships, timestamps, or automatic schema creation.
- `app/script/sync_metadata.py` is the metadata CLI Controller. It owns
  `argparse`, logging initialization, dependency assembly, and complete client
  cleanup; it does not contain storage queries.
- `app/service/metadata_sync_service.py` owns DW-schema validation, Config-to-
  Entity conversion, stable logical IDs, limits, and cross-storage sequencing.
- `app/repository/metadata_repository.py` owns concrete SQL, Qdrant, and
  Elasticsearch operations for that single flow. It accepts business Entities,
  uses ORM Models for Meta upserts, and does not reinterpret YAML roles or
  decide which fields have `sync: true`.
- `app_test/client/` mirrors `app/client/` for manager behavior and live
  integration checks. `app_test/core/` validates logging in isolation, while
  `app_test/service/` checks metadata behavior with external clients mocked.
- `conf/` contains values, while `app/conf/` contains Python models and loading
  behavior. Keep this distinction when adding a configuration field.
- `docs/docker/` is local infrastructure, not application runtime code. Keep
  service image and volume details there.

There are currently no `routes`, `views`, migrations, or generic `utils`
packages. The singular `model`, `entity`, `service`, and `repository` packages
were added for the concrete metadata flow; their existence does not require
interfaces, factories, mappers, or generic Repository base classes.

## Naming Conventions

- Python packages, modules, functions, and configuration keys use
  `snake_case`.
- Client lifecycle modules use the singular package `app/client/` and the file
  suffix `_client_manager.py`.
- Business CLI layers also use singular package names: `app/script/`,
  `app/service/`, `app/repository/`, `app/model/`, and `app/entity/`.
- Meta ORM classes retain the source-document names `TableInfoMySQL`,
  `ColumnInfoMySQL`, `MetricInfoMySQL`, and `ColumnMetricMySQL`; business
  dataclasses omit the storage suffix.
- Manager and configuration classes use `PascalCase`, for example
  `MysqlClientManager`, `TeiEmbeddingClientManager`, and `MysqlConfig`.
- Test modules mirror the runtime module name with a `test_` prefix.
- Package directories contain `__init__.py`; marker files are empty or contain
  only a package docstring. They import no application modules and perform no
  connection or configuration side effects.

## Observed External-Service Layout

Every currently configured service has the following repository-backed pieces:

1. A typed section in `app/conf/app_config.py` and a matching key in
   `conf/app_config.yaml`.
2. An async lifecycle adapter under `app/client/`.
3. A local service definition under `docs/docker/`.

Executable live checks currently exist only for MySQL and TEI, under
`app_test/client/`. Qdrant and Elasticsearch have no standalone live check.
The metadata test under `app_test/service/` is dependency-isolated and must not
be reported as a four-service integration run.

Use `.trellis/spec/backend/external-service-integrations.md` for the stricter
TEI, Qdrant, Elasticsearch, and metadata synchronization contracts;
`database-guidelines.md` for `dw`/`meta` SQL; and `logging-guidelines.md` for
Loguru behavior.
