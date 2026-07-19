# Implementation plan

## Preconditions

- Work from the task base branch with a clean worktree except for this task's
  planning artifacts.
- Before Git, branch, commit, or PR operations, read and follow
  `.agents/skills/git-pr-rules/SKILL.md`.
- Before implementation, load the backend spec index and all guides listed in
  `implement.jsonl`.
- Use the Trellis implementation/check sub-agents defined by the workflow.

## 1. Baseline and compatibility evidence

- [ ] Record `git status`, Python/uv versions, and the current validation
      results.
- [ ] Run current Ruff, Pyright, compileall, configuration, and available test
      commands before moving files.
- [ ] Capture current OpenAPI output and a normalized API contract comparison
      basis that ignores component-key names but retains paths, fields,
      required flags, enums, and status codes.
- [ ] Search active code, CI, and current specs for all `app`, `app_test`,
      `*ClientManager`, `Ddl*`, and `Llm*` references.

Rollback point: no runtime changes yet.

## 2. Packaging and persistent tool configuration

- [ ] Add a uv-compatible build backend for the `src/data_agent` package.
- [ ] Add development dependencies for Ruff, Pyright, pytest, and async pytest
      support; refresh `uv.lock`.
- [ ] Configure Ruff rule selection and Google pydocstyle convention, keeping
      all missing-public-object rules active.
- [ ] Configure pytest test paths, import mode, asyncio behavior, and
      `integration` marker.
- [ ] Configure Pyright for the `src` package and test tree if explicit config
      is required for stable imports.

Validation:

```powershell
uv sync --locked
uv lock --check
```

## 3. Shared application and infrastructure migration

- [ ] Create `src/data_agent` package markers with meaningful, side-effect-free
      Docstrings.
- [ ] Move configuration to `settings.py` and rename all settings classes.
- [ ] Move Loguru setup to `logging.py`.
- [ ] Move external resource modules to `infrastructure/`.
- [ ] Apply the exact infrastructure class mapping in `design.md`.
- [ ] Preserve lifecycle, transaction, secret-loading, and close behavior.
- [ ] Add Google Style Docstrings for every public package, module, class,
      function, and method.

Focused validation:

```powershell
uv run python -m data_agent.settings
uv run ruff check src/data_agent/infrastructure src/data_agent/settings.py
uv run pyright src/data_agent/infrastructure src/data_agent/settings.py
```

## 4. DDL metadata feature migration

- [ ] Split Pydantic contracts into `ddl_metadata/models/` without changing
      fields, aliases, validators, serialization, or enum values.
- [ ] Move errors, identifiers, parsing, and validation into the feature root.
- [ ] Move workflow graph and model adapter into `workflow/`; apply `DDL`/`LLM`
      class renames and protocol responsibility names.
- [ ] Move Redis job state into `jobs/store.py`.
- [ ] Move SQLAlchemy tables and repositories into `persistence/`.
- [ ] Split memory behavior into context, payload, snapshot, and management
      responsibilities without changing behavior.
- [ ] Move API routes into `ddl_metadata/api.py`; create `application.py` as
      composition root.
- [ ] Move arq code into `ddl_metadata/worker.py`.
- [ ] Move the executable/ASGI entry to `data_agent/main.py`.
- [ ] Rewrite all imports directly to `data_agent.*`; never introduce an
      `app.*` compatibility package.
- [ ] Add or revise public Docstrings and rationale comments.

Focused validation:

```powershell
uv run python -c "import data_agent; from data_agent.main import app"
uv run python -m compileall -q src
uv run ruff check src
uv run pyright src
```

## 5. Pytest migration

- [ ] Move `app_test` to `tests` and organize unit/integration ownership.
- [ ] Move shared fakes, factories, and fixtures out of `test_*.py` modules.
- [ ] Convert inner async coroutines plus `asyncio.run()` wrappers into native
      async pytest tests.
- [ ] Remove all test module `if __name__ == "__main__"` entry blocks.
- [ ] Mark live MySQL/Redis/TEI tests appropriately; keep paid/live LLM calls
      outside CI.
- [ ] Rename tests and imports to match the new public identifiers.
- [ ] Add concise public module/fixture/test Docstrings.

Focused validation:

```powershell
uv run pytest --collect-only
uv run pytest -m "not integration"
uv run ruff check tests
uv run pyright tests
```

## 6. API and persistence compatibility

- [ ] Compare normalized pre/post OpenAPI contracts, allowing only component
      key changes caused by class renames.
- [ ] Verify API paths, request/response fields, required flags, enums, and
      status codes are unchanged.
- [ ] Verify Redis key construction, job/checkpoint payloads, state transitions,
      and recovery behavior are unchanged.
- [ ] Verify SQLAlchemy table/column names and transaction ownership are
      unchanged.
- [ ] Search for old active import paths and renamed public identifiers.

Rollback point: if compatibility differs beyond allowed component names, fix
the migration before proceeding; do not document unintended drift as accepted.

## 7. CI and current specification migration

- [ ] Update `.github/workflows/ci.yml` to install locked dev dependencies,
      lint/type-check/compile `src` and `tests`, validate new settings, and run
      pytest.
- [ ] Update backend directory, quality, database, error, logging, external
      integration, and index guides to the new source of truth.
- [ ] Keep Trellis specification prose in English.
- [ ] Confirm active docs and CI contain no old paths or replaced class names;
      exclude archive/journal history from this rule.

## 8. Full quality gate

Run:

```powershell
uv sync --locked
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv run pytest
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

Additional checks:

- [ ] Prove deleting a public Docstring produces a Ruff missing-Docstring
      failure using a temporary/non-committed probe or Ruff rule inspection.
- [ ] Confirm no real paid LLM endpoint was contacted.
- [ ] Review the final diff for accidental business, persistence, or API
      changes.
- [ ] Run the Trellis check sub-agent and address all verified findings.

## 9. Finish

- [ ] Update `.trellis/spec/` through the Trellis spec-update workflow.
- [ ] Follow project Git rules for intentional staging and commit.
- [ ] Archive the task only after the quality gate and required commit succeed.
