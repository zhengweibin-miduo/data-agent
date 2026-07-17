# Backend Development Guidelines

> Repository-backed conventions for the current Python backend.

## Overview

This directory documents the Python application's current HTTP, workflow,
worker, persistence, configuration, async-client, error, logging, and quality
contracts. The rules are based on source code, executable checks, CI, and the
local Docker services that exist in this repository.

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

The repository now has a loopback FastAPI boundary, typed application models,
service and repository layers, a LangGraph workflow, and an arq worker. It
still has no ORM entity layer or migration framework: SQLAlchemy Core table
definitions and local bootstrap SQL own the current relational schema.

## Pre-Development Checklist

- Identify the concrete files involved: configuration belongs in
  `app/conf/app_config.py` and `conf/app_config.yaml`, async service clients in
  `app/client/`, shared contracts in `app/model/`, HTTP wiring in `app/api/`,
  orchestration in `app/service/`, bound persistence statements in
  `app/repository/`, worker recovery in `app/worker/`, logging setup in
  `app/core/`, mirrored executable checks in `app_test/`, and local
  infrastructure in `docs/docker/`.
- Read [Directory Structure](./directory-structure.md) for every backend change.
- Read [Database Guidelines](./database-guidelines.md) for MySQL, SQLAlchemy,
  repository, snapshot, or long-term-memory changes.
- Read [External Service Integrations](./external-service-integrations.md) for
  TEI, Redis, LangGraph checkpoint, or OpenAI-compatible model changes.
- Read [Error Handling](./error-handling.md) when changing API status mapping,
  job transitions, retries, or terminal cleanup.
- Trace cross-layer contract changes through Pydantic models, API/service
  consumers, Redis projections/checkpoints, repositories, and mirrored tests.

## Quality Check

- Run the Python checks recorded in
  [Quality Guidelines](./quality-guidelines.md): lock validation, Ruff, Pyright,
  `compileall`, and configuration loading.
- Run the MySQL, Redis, combined DDL-flow, or TEI live integration module only
  when the corresponding service is available, and report an unavailable
  dependency instead of claiming the check passed.
- Trace renamed package and configuration paths end to end. Current references
  use `app.client`, `app_test.client`, and `conf/app_config.yaml`; the retired
  `app.clients`, `app_test.clients`, and `conf/app.yaml` paths must not remain in
  active code, CI, or current specs. Archived task and journal records may keep
  the names that were accurate when those records were written.
- Re-read every changed guide and verify that its examples, commands, and links
  resolve to current repository files.

---

**Language**: All documentation should be written in **English**.
