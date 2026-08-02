# Verification

## Passing gates

- `cd backend && uv lock --check`: passed.
- `cd backend && uv run ruff check src tests scripts`: passed.
- `cd backend && uv run pyright src tests`: passed with 0 errors.
- `cd backend && uv run python -m compileall -q src tests scripts`: passed.
- `cd backend && uv run python -m settings`: passed.
- `cd backend && uv run pytest -m "not integration"`: 403 passed, 29 deselected.
- `cd backend && uv build`: wheel and sdist built successfully.
- Installed the wheel into a temporary non-editable virtual environment and ran
  `scripts/verify_installed_wheel.py`: passed. The check imports every declared
  top-level package/module and both console targets, confirms stdlib `logging`
  is not shadowed, and rejects `data_agent/`, `frontend/`, and `logging.py`
  paths in the wheel.
- `cd frontend && npm ci && npm run lint && npm run typecheck && npm test -- --run && npm run build`:
  passed; 10 test files and 145 tests passed, and the production build completed.
- `docker compose -f docs/docker/docker-compose.yml config`: passed.
- `git diff --check`: passed.
- Active-tree forbidden-path/import search found no `data_agent.*` Python import,
  legacy `src/data_agent/` source path, or `ENABLE_LEGACY_FRONTEND` reference.
  Remaining `data_agent` strings are approved external identifiers such as the
  SQL bootstrap filename, database/schema/user, environment variable, logging
  identifier, memory source, CLI, or distribution name.

## Environment-owned integration result

`cd backend && uv run pytest -m "not tei"` collected 432 tests and completed
with 429 passed, 1 deselected, and 2 failed. Both failures have the same
pre-existing shared-database cause:

```text
Unknown column 'fact_table_id' in 'field list'
```

Affected tests:

- `tests/integration/persistence/test_metadata_repository.py::test_snapshot_expires_removed_column_and_metric_memories`
- `tests/integration/test_ddl_metadata_flow.py::test_ddl_metadata_flow`

The checked-in fresh-environment `docs/docker/mysql/meta.sql` owns the current
schema, but the shared local MySQL volume has not been recreated or migrated.
This task does not authorize database migration, reset, or cleanup, so no
external state was changed.

## Review result

The final full-scope review covered the PRD, design, implementation plan,
`code_review.md`, backend/frontend spec indexes, packaging, CI, documentation,
forbidden namespace/path searches, installed-wheel behavior, and the existing
package-boundary tests. No merge-blocking P0/P1 code issue was found. The
required `trellis-check` sub-agent was started, exceeded the project ten-minute
limit without returning a result, and was stopped; the primary agent completed
the review directly.
