# Backend Quality Guidelines

## Runtime and Dependency Baseline

- Python is constrained to `>=3.13,<3.14` in `pyproject.toml`, and
  `.python-version` pins `3.13`.
- The distribution is installed from `src/data_agent/`; direct imports use
  `data_agent`, never the retired `app` package or repository-root import
  accidents.
- Runtime and development dependencies and their resolved versions are managed
  by `uv`, `pyproject.toml`, and `uv.lock`.
- Runtime and test packages use explicit type annotations and Chinese Google
  Style Docstrings. Ruff enforces Docstrings for public packages, modules,
  classes, functions, methods, fixtures, and tests.
- Review findings from Codex, Trellis check agents, and other AI reviewers must
  be written in Simplified Chinese per `AGENTS.md`; identifiers, paths,
  commands, keys, and original errors remain in English.

## Required Local Patterns

- Configuration models inherit `SettingsModel`, whose
  `ConfigDict(extra="forbid")` rejects unknown fields.
- Shared infrastructure resources use typed `ClassVar[ClientType | None]`
  state, idempotent `initialize()`, guarded `get_client()`, and async
  `close()`.
- Package `__init__.py` files are documented and side-effect free.
- HTTP, model, graph, Redis, and persistence boundaries reuse contracts from
  `data_agent.ddl_metadata.models`; consumers do not cast shared JSON payloads
  independently.
- Unit graph/model tests use deterministic fakes and never require a live LLM.
  Integration tests clearly mark MySQL, Redis, and TEI requirements.
- Repository integration data uses UUID-derived sources/stable IDs and scoped
  cleanup. Tests never reset or delete the shared developer Docker volume.
- Async tests are native pytest async tests. Do not add `asyncio.run()`
  wrappers or test-module `if __name__ == "__main__"` entry points.
- Test result checks use `tests.helpers.checks.check_equal()` or
  `check_condition()` so every check emits a labeled `PASS` / `FAIL` record
  with actual and expected values before `pytest.fail()` blocks a regression.
  Use `fail_check()` when an expected exception or other required branch does
  not occur. Do not add bare `assert` statements to `tests/`.
- Keep pytest's default output capture for CI and routine runs. Use
  `uv run pytest -s ...` only when a developer needs to observe every check
  result live; visible output must complement, never replace, automatic
  failure semantics.
- Tests requiring live MySQL or Redis use the `integration` marker. The
  optional TEI live test uses both `integration` and `tei`; CI excludes `tei`
  unless that service is explicitly provisioned. Reusable fakes and factories
  live outside `test_*.py` modules.

## Docstring and Comment Contract

- Use PEP 257 structure with Google Style sections and Chinese prose.
- Keep section names such as `Args:`, `Returns:`, `Yields:`, and `Raises:` in
  English.
- Do not repeat types already expressed by annotations.
- One-line Docstrings are appropriate for simple public objects. Document
  non-obvious arguments, results, exceptions, side effects, transactions,
  concurrency, and lifecycle constraints when applicable.
- Inline comments explain rationale and invariants, not visible code behavior.
- English-only imperative-mood and terminal-punctuation rules may be disabled
  for Chinese prose; missing-public-object Ruff rules must remain enabled.

## Validation Commands

The repository CI in `.github/workflows/ci.yml` defines the baseline:

```powershell
uv sync --locked
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv run pytest -m "not tei"
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

Use `uv run pytest -m "not integration"` for checks that do not require live
services. CI runs `uv run pytest -m "not tei"` after MySQL and Redis are ready.
Run the focused TEI test explicitly when that optional local service is
available.

Before persistence integration tests, CI applies
`docs/docker/mysql/data_agent.sql` through the root account. Developers reusing
an initialized Compose volume must do the same once because MySQL entrypoint
bootstrap scripts run only for an empty volume. This command creates/grants the
application database and its current memory tables idempotently; it must not be
replaced with destructive cleanup of Meta tables.

No CI test contacts a live LLM. The LLM infrastructure test mocks the
capability probe; the real worker startup probe is a separate deployment check.

`pyproject.toml` persists Ruff, Pyright, and pytest configuration. Ruff uses
the Google pydocstyle convention, pytest collects from `tests/` with async
support, and the installed `data_agent` package is the runtime import target.

## Review Checklist

- Trace configuration changes across `conf/app_config.yaml`,
  `src/data_agent/settings.py`, every consumer, and configuration validation.
- Verify that a new infrastructure client follows the established lifecycle
  and closes the exact underlying async resource.
- Confirm tests exercise real behavior rather than only checking object shape;
  MySQL runs live transactions, Redis checks atomic state, the integration flow
  uses real Redis checkpoints plus MySQL persistence, and TEI checks vector
  dimensions and normalization.
- For parser/LLM work, prove unsupported SQL rejects before a model call and
  physical objects cannot be added, removed, renamed, or retyped by model
  output.
- For graph/worker work, prove interrupt/resume revision safety and that a
  persistence retry does not repeat completed model calls.
- For persistence/memory work, prove scoped cleanup, rollback, exact compatible
  reuse, soft-delete exclusion, update events, and outbox replay.
- Verify pytest collection uses `tests/` and the installed `data_agent`
  package, not a repository-root fallback import.
- Verify every public runtime and test object has a meaningful Docstring; do
  not satisfy Ruff with restatements such as "X class."
- Run checks relevant to the changed service and report unavailable live
  dependencies explicitly.

## Forbidden Patterns

- Unknown or silently ignored configuration fields.
- Unannotated shared client state or a shared client typed as `Any`.
- Sync client calls inside the established async infrastructure layer.
- Resource acquisition without a corresponding async close path.
- Tests that leak a client when an assertion or request fails.
- Tests that call a real paid/model endpoint in CI.
- Destructive integration cleanup against the shared MySQL or Redis volume.
- `asyncio.run()` wrappers or executable main guards in pytest modules.
- Missing or placeholder public Docstrings.
- Claims that a skipped or unavailable live-service check passed.
