---
goal: Migrate the repository to independent backend/src and frontend/src roots without a data_agent Python namespace
version: 1.0
date_created: 2026-08-02
last_updated: 2026-08-02
owner: zwb
status: Planned
tags: [architecture, backend, frontend, packaging, imports]
---

# Implementation Plan

## Phase 1: Establish the backend root

- Move `src/data_agent/` contents directly to `backend/src/`, excluding the legacy frontend directory.
- Rename top-level `logging.py` to `app_logging.py`.
- Move `tests/`, `conf/`, `pyproject.toml` and `uv.lock` to `backend/`; add `backend/README.md` and `frontend/README.md`.
- Keep `.github/`, `docs/docker/`, root README and repository tooling at root.
- Rollback point: no import rewrites are accepted while both old and new source trees coexist.

## Phase 2: Hard-migrate Python imports and packaging

- Rewrite active source/test imports and dynamic module strings from `data_agent.*` to direct top-level modules.
- Update monkeypatch targets, package-boundary tests and hard-coded source paths.
- Configure setuptools with explicit packages and `py-modules`; update scripts to `main:main` and `data_sync.worker:main`.
- Update configuration fallback for `backend/conf/app_config.yaml` while preserving `DATA_AGENT_CONFIG`.
- Add an installed-wheel smoke check covering every top-level package, entry point and standard-library `logging` identity.

## Phase 3: Remove backend-owned frontend compatibility

- Delete the legacy frontend assets, switch parser, static mount/routes and compatibility tests.
- Keep API-only/CORS tests and verify no wheel file or backend source references frontend assets.

## Phase 4: Update repository consumers

- Update CI backend working directories, cache/lock paths, SQL/bootstrap references and commands.
- Update root/backend/frontend README files, AGENTS source-root rules, CONTEXT-MAP links and current Trellis backend/frontend specs.
- Do not rewrite archived tasks/journals or external stable identifiers that merely contain `data_agent`.

## Phase 5: Verification

From `backend/`:

```text
uv lock --check
uv sync --locked
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m settings
uv run pytest -m "not integration"
uv build
```

Additional backend checks:

- install the built wheel into an isolated temporary environment and import all declared packages/modules;
- execute both console entry points or their bounded smoke paths;
- assert imported standard-library `logging` is not sourced from `backend/src`;
- inspect wheel contents and assert no legacy frontend assets or `data_agent/` path exists;
- search active source, tests, CI, scripts and current specs for forbidden `data_agent.*`, old roots and legacy switch references.

From `frontend/`:

```text
npm ci
npm run lint
npm run typecheck
npm run test
npm run build
```

From repository root:

```text
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

Run relevant MySQL/Redis/CDC integration tests from `backend/` when services are available. Record the known shared `metric_info.fact_table_id` schema drift without modifying the shared database unless separately authorized.

## Review Gates

- No second source tree, compatibility namespace or wildcard package discovery.
- No inner-layer imports of infrastructure/adapters introduced by the rewrite.
- No frontend files in backend source or distributions.
- No tests added solely for moved private helpers; update only path/entry contracts and retain behavior seam coverage.
- Independent P0/P1 review follows `code_review.md` before completion.
