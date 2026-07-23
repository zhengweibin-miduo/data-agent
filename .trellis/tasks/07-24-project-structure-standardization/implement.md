# Runtime Assembly Implementation Plan

## Implementation

- [x] Add `src/data_agent/runtime.py` with typed public `RuntimeRole`,
  `RuntimeHandle`, `start()`, and `stop()` objects using Chinese Google Style
  Docstrings.
- [x] Implement private ordered role plans and private State/mapping publication.
- [x] Implement startup rollback that attempts every completed close action,
  logs safe rollback failures, drains Loguru, and re-raises the original startup
  exception.
- [x] Implement best-effort normal shutdown, single-error identity preservation,
  multi-error `ExceptionGroup`, and repeated-stop protection.
- [x] Replace duplicated API lifecycle assembly in `application.py` with the
  runtime interface while preserving FastAPI state and lifecycle logs.
- [x] Replace duplicated Worker lifecycle assembly with the runtime interface;
  store the handle under `ctx["_runtime_handle"]` and preserve all existing
  role-specific startup behavior.
- [x] Update lifecycle tests to cover the public runtime interface, state keys,
  exact order, rollback, rollback failure, best-effort shutdown, multiple close
  failures, invalid handles, and Loguru draining.
- [x] Update `.trellis/spec/backend/directory-structure.md`,
  `.trellis/spec/backend/error-handling.md`, and
  `.trellis/spec/backend/logging-guidelines.md` to match the new owner and
  approved failure semantics.

## Focused validation

```powershell
uv run pytest tests/unit/infrastructure/test_logging_lifecycle.py
uv run pytest -m "not integration" -k "lifecycle or runtime"
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
git diff --check
```

## Full validation

```powershell
uv lock --check
uv run python -m data_agent.settings
uv run pytest -m "not tei"
docker compose -f docs/docker/docker-compose.yml config
```

Live-service checks may run only when their dependencies are available. Report
unavailable services instead of claiming success.

## Risk and rollback points

- Preserve exact state keys before changing either entry point.
- Preserve worker index-deferred handling and maintenance order before deleting
  duplicated code.
- Do not expose the private action registry or concrete clients.
- Verify exception identity for single startup/shutdown failures before
  accepting `ExceptionGroup` behavior.
- If entry-point integration regresses, restore both old lifecycle bodies and
  remove `runtime.py`; no data rollback is required.

