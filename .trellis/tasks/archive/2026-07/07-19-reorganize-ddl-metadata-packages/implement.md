# Implementation Plan

## 1. Capture Baseline Contracts

- [ ] Record current FastAPI route metadata.
- [ ] Record `WorkerSettings` functions, cron schedules, and runtime values.
- [ ] Add or strengthen static/unit assertions for Redis keys, codecs, Lua
  scripts, job names, LangGraph topology/state keys, SQLAlchemy tables, and
  configuration/logging contracts where current coverage is indirect.
- [ ] Run the deterministic baseline checks before moving code.

Validation:

```powershell
uv lock --check
uv run pytest tests/unit
uv run python -m compileall -q src tests
```

## 2. Reorganize Jobs and API

- [ ] Create `jobs/redis/` and move Redis keys, codec, scripts, state, outbox,
  lease, and base typing support.
- [ ] Move the application-facing facade to `jobs/store.py`.
- [ ] Move `question_set_id()` to `jobs/identifiers.py`.
- [ ] Split job and memory HTTP routes under `api/` and add the aggregate
  router.
- [ ] Update application, workflow, tests, and current specs to concrete paths.
- [ ] Delete retired flat modules after a stale-path search.

Validation:

```powershell
uv run pytest tests/unit/ddl_metadata -k "job"
uv run pytest tests/integration/test_api.py tests/integration/test_worker.py
uv run python -m compileall -q src tests
```

Rollback point: jobs/API package migration.

## 3. Reorganize Memory Domain and Indexing

- [ ] Move payload helpers to `memory/domain/payloads.py`.
- [ ] Split accepted-candidate construction from snapshot persistence.
- [ ] Split RRF from search orchestration.
- [ ] Move application use cases under `memory/application/`.
- [ ] Split Elasticsearch, Qdrant, dispatcher, and rebuilder under
  `memory/indexing/`.
- [ ] Update workflow, worker, application, and test imports.

Validation:

```powershell
uv run pytest tests/unit/ddl_metadata -k "memory or graph"
uv run pytest tests/integration/test_memory_services.py
uv run python -m compileall -q src tests
```

Rollback point: memory domain/application/indexing migration.

## 4. Reorganize Memory MySQL Persistence

- [ ] Introduce the shared SQLAlchemy metadata owner.
- [ ] Move memory tables to `memory/mysql/tables.py`.
- [ ] Move and split authoritative-memory versus index-outbox persistence.
- [ ] Preserve one engine, one Session, schema qualification, and atomic Meta
  plus memory commits.
- [ ] Update integration fixtures and repository tests.
- [ ] Delete retired persistence paths after static search.

Validation:

```powershell
uv run pytest tests/integration/persistence
uv run pytest tests/integration/test_ddl_metadata_flow.py
uv run python -m compileall -q src tests
```

Rollback point: MySQL persistence package migration.

## 5. Split Workflow and Worker

- [ ] Extract workflow state and dependency contracts.
- [ ] Extract dependency-bound nodes and pure routing.
- [ ] Keep graph builder limited to node/edge registration and compilation.
- [ ] Separate LLM metadata generator implementation from its protocol.
- [ ] Split worker job runner, maintenance, lifecycle, and settings.
- [ ] Update arq discovery references and worker tests.
- [ ] Compare graph and worker contract snapshots with the baseline.

Validation:

```powershell
uv run pytest tests/unit/ddl_metadata/workflow/test_graph.py
uv run pytest tests/integration/test_worker.py
uv run pytest tests/integration/test_ddl_metadata_flow.py
uv run python -m compileall -q src tests
```

Rollback point: workflow/worker package migration.

## 6. Repository-Wide Consistency Pass

- [ ] Re-audit every production module and record the cohesion rationale for
  intentionally retained files.
- [ ] Mirror relevant unit-test package structure.
- [ ] Search active source, tests, README, configuration, and current specs for
  all retired paths.
- [ ] Update `.trellis/spec/backend/directory-structure.md`,
  database/external-service/quality/error guides where paths changed.
- [ ] Inspect Docker/SQL/config assets for behavior drift; do not edit when no
  contract changed.

## 7. Quality and Review Gates

- [ ] Run `trellis-check` through the required checking agent.
- [ ] Read and apply `code_review.md` to every AI review finding.
- [ ] Run:

```powershell
uv lock --check
uv run ruff check .
uv run pyright
uv run python -m compileall -q src tests
uv run python -c "from data_agent.settings import app_config; print(app_config.api.host)"
uv run pytest tests/unit
uv run pytest tests/integration
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

- [ ] Report unavailable external services precisely and retain evidence from
  all checks that did run.
- [ ] Complete a requirement-by-requirement audit against PRD AC1-AC8.

## Risks

- Import cycles between workflow contracts/nodes and memory application code.
- Accidental Redis field/Lua changes during mechanical moves.
- Losing the shared SQLAlchemy `MetaData` owner and cross-database transaction.
- arq discovery path drift after converting `worker.py` into a package.
- LangGraph node/state/checkpoint drift during node extraction.

These are addressed by the baseline contract snapshots and phase-local
validation before deleting retired modules.
