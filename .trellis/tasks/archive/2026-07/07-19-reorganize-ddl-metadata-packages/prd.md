# Reorganize Project Responsibilities and Package Structure

## Goal

Audit the entire `data-agent` repository and reorganize production packages so
that each package communicates a coherent responsibility. The refactor must
make infrastructure-specific implementations, application orchestration,
domain logic, transport boundaries, and runtime entry points easier to locate
without changing externally observable behavior.

## Background

- The previous DDL job-store split separated classes into files, but Redis
  implementation details remain flat beside the application-facing
  `DDLJobStore` facade under `src/data_agent/ddl_metadata/jobs/`.
- The user expects Redis-specific job storage code to be grouped under a
  package such as `ddl_metadata/jobs/redis/`.
- `ddl_metadata/memory/` currently mixes deterministic memory projections,
  application services, MySQL repository dependencies, search orchestration,
  outbox processing, and Elasticsearch/Qdrant index adapters.
- The audit scope is the whole repository, explicitly including API routes,
  worker runtime, workflow composition, application composition,
  infrastructure adapters, models, persistence, tests, configuration, and
  current project specifications.

## Requirements

- R1. Inventory every production module under `src/data_agent/`, identify its
  current responsibilities and dependencies, and distinguish real
  responsibility mixing from files that are already cohesive.
- R2. Group Redis-specific DDL job storage implementations beneath a dedicated
  `ddl_metadata/jobs/redis/` package while keeping an application-facing job
  service/facade outside that infrastructure package.
- R3. Reorganize memory code into explicit responsibility boundaries for
  deterministic/domain behavior, application orchestration, persistence, and
  external search-index infrastructure where the current code supports those
  distinctions.
- R4. Review API, worker, and workflow modules for oversized or mixed
  responsibilities and split or regroup them when evidence shows independently
  named responsibilities.
- R5. Review the remaining project modules, including `application.py`,
  `settings.py`, `logging.py`, `infrastructure/`, `models/`, `persistence/`,
  parsing, validation, identifiers, entry points, tests, configuration, and
  Docker assets for the same package-ownership problem.
- R6. Preserve HTTP paths and response contracts, arq function registration and
  job names, Redis keyspace and serialized payloads, MySQL schema and queries,
  LangGraph state/checkpoint compatibility, configuration keys, logging event
  names, and runtime behavior unless a behavior change is explicitly approved.
- R7. Perform a hard internal import migration: update production code, tests,
  current specifications, and active documentation to the final package paths;
  do not keep compatibility shims solely for retired internal paths.
- R8. Avoid speculative abstraction and directory-only nesting. Every new
  package must have a clear owner, multiple cohesive members or a justified
  boundary, and a dependency direction that can be stated and checked.
- R9. Keep package `__init__.py` files side-effect free and use them only for
  meaningful package documentation, consistent with the current backend spec.
- R10. Update affected tests and project specifications so the documented
  directory layout and dependency rules match the implemented structure.

## Acceptance Criteria

- [x] AC1. A repository-wide responsibility inventory covers every Python
  module under `src/data_agent/` and records keep/move/split decisions with
  evidence.
- [x] AC2. Redis-specific job implementation code resides under
  `ddl_metadata/jobs/redis/`; application-facing job orchestration does not
  expose Redis implementation modules to API, worker, workflow, or memory
  consumers.
- [x] AC3. Memory modules have explicit and internally consistent package
  ownership; database repositories and external index adapters are not mixed
  with deterministic memory transformation code.
- [x] AC4. API, worker, workflow, and all remaining production modules have been
  audited, with each identified responsibility problem either corrected or
  explicitly retained with a concrete cohesion rationale.
- [x] AC5. No active source, test, README, configuration, or current Trellis
  spec references a retired internal import path or obsolete directory tree.
- [x] AC6. HTTP, arq, Redis, MySQL, LangGraph, configuration, and logging
  compatibility constraints in R6 are verified by targeted tests or static
  contract checks.
- [x] AC7. `uv lock --check`, Ruff, Pyright, `compileall`, configuration
  loading, all deterministic/unit tests, and applicable integration tests pass;
  unavailable external services are reported rather than claimed as passing.
- [x] AC8. The final backend directory-structure specification describes the
  implemented package tree, ownership, and dependency direction.

## Out of Scope

- New product features or changes to public API semantics.
- Redis key, MySQL schema, LangGraph checkpoint, or configuration migrations
  unless current code proves one is unavoidable for a package-only refactor.
- Introducing a generic `common`, `utils`, `manager`, or cross-feature
  abstraction without at least one demonstrated cross-feature contract.
