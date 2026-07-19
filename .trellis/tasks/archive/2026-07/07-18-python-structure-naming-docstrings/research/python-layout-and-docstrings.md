# Python layout, testing, naming, and Docstring research

## External guidance

### Source layout

- PyPA describes `src/` layout as separating importable code from repository
  root files and preventing accidental imports from the working tree:
  <https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/>
- pytest recommends `src/` for new projects, tests outside application code,
  and `--import-mode=importlib`:
  <https://docs.pytest.org/en/latest/explanation/goodpractices.html>
- uv packaged applications use `src/<import_package>/` and a declared build
  backend:
  <https://docs.astral.sh/uv/concepts/projects/init/>

### FastAPI organization

- FastAPI's larger-application guide separates application composition from
  routers while leaving internal business organization to the application:
  <https://fastapi.tiangolo.com/tutorial/bigger-applications/>
- For this repository, the majority of runtime behavior belongs to one
  `ddl_metadata` capability. A direct feature package plus a shared
  `infrastructure` package provides clearer ownership than the current
  repository-wide horizontal layers without introducing full DDD/CQRS depth.

### Naming

- PEP 8 requires short lowercase package/module names, `snake_case` functions
  and variables, `CapWords` classes, and recommends preserving all letters in
  acronyms such as `HTTPServerError`:
  <https://peps.python.org/pep-0008/>
- Apply the same rule to established project acronyms: `DDL`, `LLM`, `API`,
  `TEI`, and the product spelling `MySQL`.
- Prefer names that expose capability (`Repository`, `Store`, `Client`,
  `Service`, `Loader`) over generic implementation-role names such as
  `Manager`, `Helper`, or `Utils`.

### Docstrings and comments

- PEP 257 defines public module/class/function/method coverage, triple
  double-quoted Docstrings, one-line summaries, and multi-line summary/body
  separation:
  <https://peps.python.org/pep-0257/>
- Google Python Style provides readable `Args`, `Returns`, `Yields`, and
  `Raises` sections and emphasizes comments that explain non-obvious design:
  <https://google.github.io/styleguide/pyguide.html>
- Ruff supports Google, NumPy, and PEP 257 conventions and enables pydocstyle
  rules through the `D` prefix:
  <https://docs.astral.sh/ruff/faq/>
- This project will use Chinese prose with Google section headers. English-only
  imperative-mood and terminal-punctuation rules may be disabled, but missing
  public package/module/class/function/method Docstrings remain enforced.

## Repository evidence

- Runtime source currently lives under `app/`; tests live under `app_test/`.
- `pyproject.toml` declares project metadata and dependencies but no build
  backend, Ruff configuration, pytest configuration, or development dependency
  group.
- CI runs Ruff and Pyright via temporary `uv --with` dependencies and executes
  each test module with `python -m`.
- Current tests already expose pytest-discoverable `test_*` functions, but
  asynchronous behavior is wrapped in inner coroutines plus `asyncio.run()`.
- Tests import shared fakes from other `test_*.py` modules; those definitions
  must move to `conftest.py` or `tests/helpers/`.
- Current Trellis backend guides explicitly prescribe `app/`, `app_test/`,
  `app.client`, `*ClientManager`, and old validation commands. Current guides
  must be updated with the implementation; archived tasks and journals remain
  historical records.

## Selected direction

- Use one atomic task because source moves, import rewrites, class renames,
  test conversion, Ruff enforcement, CI, and current specs form one coupled
  import/quality migration. Splitting them into separately active child tasks
  would intentionally leave the repository broken between children.
- Use a hard cutover to `data_agent.*`; do not ship an `app.*` compatibility
  package.
- Allow OpenAPI component names derived from renamed Pydantic classes to
  change, while preserving API paths, fields, status codes, and JSON behavior.
- Convert test execution to pytest with async support and explicit integration
  markers.

## Pre-migration baseline

Captured immediately before implementation on 2026-07-18:

- `uv lock --check`: passed.
- Ruff against `app app_test`: passed.
- Pyright against `app app_test`: passed with zero errors and warnings.
- `compileall` and `python -m app.conf.app_config`: passed.
- Focused logging, mocked LLM, parser, validator, and graph modules: passed.
- OpenAPI contained 6 paths and 31 schemas.
- Paths:
  - `/api/v1/metadata/ddl-jobs`
  - `/api/v1/metadata/ddl-jobs/{job_id}`
  - `/api/v1/metadata/ddl-jobs/{job_id}/answers`
  - `/api/v1/metadata/memories`
  - `/api/v1/metadata/memories/{memory_uid}`
  - `/api/v1/metadata/memories/{memory_uid}/corrections`
- The pre-migration schema component set included `DdlJobRequest` and
  `DdlJobAccepted`; these two component keys are explicitly allowed to become
  `DDLJobRequest` and `DDLJobAccepted`. Paths, operations, fields, required
  flags, enum values, status codes, and JSON behavior are not allowed to drift.
