# Backend Development Guidelines

> Repository-backed conventions for the current Python backend.

## Overview

This directory documents the Python application's current package boundaries,
configuration, CLI synchronization flow, async client lifecycle, persistence
scope, error behavior, and quality gates. The rules are based on the source,
executable checks, CI, and local Docker services that exist in this repository.

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Current module organization, including the MVC-like CLI flow | Defined |
| [Database Guidelines](./database-guidelines.md) | Async SQLAlchemy lifecycle and `dw`/`meta` synchronization | Defined |
| [Error Handling](./error-handling.md) | CLI failures, propagation, and complete async cleanup | Defined |
| [Quality Guidelines](./quality-guidelines.md) | CI checks, synchronization tests, and review standards | Defined |
| [Logging Guidelines](./logging-guidelines.md) | Loguru sinks, formats, context and safety | Defined |
| [External Service Integrations](./external-service-integrations.md) | Executable contracts for TEI, Qdrant, Elasticsearch, and MySQL | Defined |

## Scope Boundary

The repository has one established business flow: the metadata synchronization
CLI under `app/script/`, `app/service/`, and `app/repository/`. It still has no
HTTP routes, migration framework, scheduler, or worker package. Metadata sync
adds the concrete ORM mappings under `app/model/` and business dataclasses under
`app/entity/`; these layers are scoped to that flow rather than a generic data
framework.

## Pre-Development Checklist

- Identify the concrete files involved: shared infrastructure configuration
  belongs in `app/conf/app_config.py` and `conf/app_config.yaml`; metadata sync
  configuration belongs in `app/conf/meta_config.py` and
  `conf/meta_config.yaml`; async clients live in `app/client/`; the metadata
  Controller, Service, and Repository live in `app/script/`, `app/service/`,
  and `app/repository/`; Meta MySQL mappings live in `app/model/`, business
  transfer objects live in `app/entity/`, and local infrastructure stays in
  `docs/docker/`.
- Read [Directory Structure](./directory-structure.md) for every backend change.
- Read [Database Guidelines](./database-guidelines.md) for MySQL or SQLAlchemy
  changes and [External Service Integrations](./external-service-integrations.md)
  for TEI, Qdrant, Elasticsearch, or metadata synchronization changes.
- Confirm that a proposed layer or convention already exists in the repository;
  the absent layers listed above have no established project rules yet. Do not
  generalize the metadata-specific Repository into a framework without a second
  concrete use case.

## Quality Check

- Run the Python checks recorded in
  [Quality Guidelines](./quality-guidelines.md): lock validation, Ruff, Pyright,
  `compileall`, and configuration loading.
- Run `app_test.service.test_metadata_sync_service` for metadata changes. Run
  the live synchronization command only when MySQL, Qdrant, Elasticsearch, and
  TEI are all available and writable; otherwise report the unavailable
  dependencies instead of claiming the integration passed.
- Trace renamed package and configuration paths end to end. Current references
  use `app.client`, `app_test.client`, and `conf/app_config.yaml`; the retired
  `app.clients`, `app_test.clients`, and `conf/app.yaml` paths must not remain in
  active code, CI, or current specs. Archived task and journal records may keep
  the names that were accurate when those records were written.
- Re-read every changed guide and verify that its examples, commands, and links
  resolve to current repository files.

---

**Language**: All documentation should be written in **English**.
