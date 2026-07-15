# Backend Development Guidelines

> Repository-backed conventions for the current Python backend.

## Overview

This directory documents the Python application's current package boundaries,
configuration, async client lifecycle, database scope, error behavior, and
quality gates. The rules are based on the source, live integration checks, CI,
and local Docker services that exist in this repository.

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Current module organization and file layout | Defined |
| [Database Guidelines](./database-guidelines.md) | Existing async SQLAlchemy scope and known absences | Defined |
| [Error Handling](./error-handling.md) | Lifecycle errors, propagation, and cleanup | Defined |
| [Quality Guidelines](./quality-guidelines.md) | CI checks, test patterns, and review standards | Defined |
| [Logging Guidelines](./logging-guidelines.md) | Loguru sinks, formats, context and safety | Defined |
| [External Service Integrations](./external-service-integrations.md) | Executable contracts for local infrastructure clients | Defined |

## Scope Boundary

The repository has no API routes, business-service package, ORM models, or
migrations yet. The relevant guides record those absences instead of defining
hypothetical conventions.

## Pre-Development Checklist

- Identify the concrete files involved: configuration belongs in
  `app/conf/app_config.py` and `conf/app_config.yaml`, async service clients in
  `app/client/`, logging setup in `app/core/`, live integration checks in
  `app_test/client/`, and local infrastructure in `docs/docker/`.
- Read [Directory Structure](./directory-structure.md) for every backend change.
- Read [Database Guidelines](./database-guidelines.md) for MySQL or SQLAlchemy
  changes, [External Service Integrations](./external-service-integrations.md)
  for TEI changes, and the other topic guide that matches the files being
  changed.
- Confirm that a proposed layer or convention already exists in the repository;
  the absent layers listed above have no established project rules yet.

## Quality Check

- Run the Python checks recorded in
  [Quality Guidelines](./quality-guidelines.md): lock validation, Ruff, Pyright,
  `compileall`, and configuration loading.
- Run the MySQL or TEI live integration module only when the corresponding
  service is available, and report an unavailable dependency instead of
  claiming the check passed.
- Trace renamed package and configuration paths end to end. Current references
  use `app.client`, `app_test.client`, and `conf/app_config.yaml`; the retired
  `app.clients`, `app_test.clients`, and `conf/app.yaml` paths must not remain in
  active code, CI, or current specs. Archived task and journal records may keep
  the names that were accurate when those records were written.
- Re-read every changed guide and verify that its examples, commands, and links
  resolve to current repository files.

---

**Language**: All documentation should be written in **English**.
