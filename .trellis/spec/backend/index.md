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
| [Conversation and Long-Term User Memory](./conversation-memory.md) | Permanent text conversations, async extraction, and tenant-scoped recall | Defined |
| [Error Handling](./error-handling.md) | Lifecycle errors, propagation, and cleanup | Defined |
| [Quality Guidelines](./quality-guidelines.md) | CI checks, test patterns, and review standards | Defined |
| [Logging Guidelines](./logging-guidelines.md) | Loguru sinks, formats, context and safety | Defined |
| [External Service Integrations](./external-service-integrations.md) | Executable contracts for local infrastructure clients | Defined |

## Scope Boundary

The repository now has a loopback FastAPI boundary, typed application models,
feature-owned services and persistence, a LangGraph workflow, and an arq
worker. Runtime code is installed from `src/data_agent/`; tests use pytest
under `tests/`. It still has no ORM entity layer or migration framework:
SQLAlchemy Core table definitions and local bootstrap SQL own the current
relational schema.

## Pre-Development Checklist

- Identify the concrete files involved: configuration belongs in
  `src/data_agent/settings.py` and `conf/app_config.yaml`, cross-feature
  contracts in `src/data_agent/models/`, long-term memory in
  `src/data_agent/memory/`, shared SQLAlchemy metadata in
  `src/data_agent/persistence/schema.py`, shared async resources in
  `src/data_agent/infrastructure/`, DDL-specific behavior in
  `src/data_agent/ddl_metadata/`, application composition in
  `src/data_agent/application.py`, logging setup in
  `src/data_agent/logging.py`, pytest checks in `tests/`, and local
  infrastructure in `docs/docker/`.
- Read [Directory Structure](./directory-structure.md) for every backend change.
- Read [Database Guidelines](./database-guidelines.md) for MySQL, SQLAlchemy,
  repository, snapshot, or long-term-memory changes.
- Read [Conversation and Long-Term User Memory](./conversation-memory.md) for
  conversation, turn, context, extraction, or user-memory recall changes.
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
  use `data_agent.models`, `data_agent.memory`, `data_agent.persistence`,
  `data_agent.infrastructure`, `data_agent.ddl_metadata`, `tests`, and
  `conf/app_config.yaml`. Retired `app`, `app_test`, root `main.py`, and
  feature-nested shared-contract paths must not remain in active code, CI, or
  current specs. Archived task and journal records may keep names that were
  accurate when those records were written.
- Re-read every changed guide and verify that its examples, commands, and links
  resolve to current repository files.

---

**Language**: All documentation should be written in **English**.
