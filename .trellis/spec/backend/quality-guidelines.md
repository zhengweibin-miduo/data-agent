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
- Every configuration field uses `Field(description="...")` with a concrete
  Chinese business explanation. Preserve defaults and validation constraints
  in the same `Field`; `tests/unit/test_settings.py` recursively rejects
  missing or non-Chinese descriptions across root and nested settings models.
- Every field on a shared `ContractModel` uses `Field(description="...")` with
  a non-empty Chinese business explanation. Preserve the field's type, default,
  and validation constraints while adding the description; the regression test
  `tests/unit/test_model_descriptions.py` covers all domain model modules.
- Runtime configuration modules contain definitions, validators, loaders, and
  shared instances only. Default-configuration self-checks belong in pytest,
  not in a production-module `if __name__ == "__main__"` assertion block.
- Shared infrastructure resources use typed `ClassVar[ClientType | None]`
  state, idempotent `initialize()`, guarded `get_client()`, and async
  `close()`.
- Package `__init__.py` files are documented and side-effect free.
- HTTP, model, graph, Redis, and persistence boundaries reuse contracts from
  `data_agent.models`; consumers do not cast shared JSON payloads
  independently.
- Unit graph/model tests use deterministic fakes and never require a live LLM.
  Integration tests clearly mark MySQL, Redis, and TEI requirements.
- Repository integration data uses UUID-derived sources/stable IDs and scoped
  cleanup. Tests never reset or delete the shared developer Docker volume.
- Async tests are native pytest async tests. Do not add `asyncio.run()`
  wrappers or test-module `if __name__ == "__main__"` entry points.
- Material synchronous parsing reached from an async runtime must expose only
  an async public boundary. `ddl_metadata.parsing.parse_ddl()` awaits
  `asyncio.to_thread(_parse_ddl_sync, ...)`, and the private callable owns the
  complete SQLGlot parse, AST projection, canonicalization, hashing, and model
  construction pipeline. Do not add a public synchronous alias or move only
  part of that pipeline off the event loop.
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
- Business flows, including CRUD methods, use concise numbered Chinese step
  comments at filtering, validation, read, write, state-transition,
  transaction, persistence, and read-back boundaries. Production code does not
  keep standalone unnumbered explanatory comments: preserve their useful
  rationale by merging it into the relevant numbered step, or delete it when
  redundant. Tool directives such as `noqa`, type-ignore, and coverage comments
  are exempt. The comments must let a reader reconstruct the flow without
  narrating every expression, and must not use `TODO` for behavior that is
  already implemented.
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

## Scenario: User-triggered Codex CI Fix

### 1. Scope / Trigger

- A PR author explicitly requests repair by posting the exact `/codex-fix-ci`
  comment. CI failure alone never starts a Codex repair loop.

### 2. Signatures

- Command: `/codex-fix-ci`
- Event: `issue_comment.created` on a pull request.

### 3. Contracts

- Both the PR author and command author must be listed in `PR_AUTHORS`.
- The PR must be non-draft, use a same-repository head, and still point to the
  inspected head SHA.
- `CODEX_TRIGGER_TOKEN` is required to create the `@codex` delegation comment.
- Only completed failed `pull_request` runs associated with the current PR and
  head SHA are delegated.

### 4. Validation & Error Matrix

- Non-exact command, ineligible PR, or unauthorized user -> no delegation.
- No matching failed run -> no delegation.
- Head changes during inspection -> stop without delegation.
- Existing marker for the same head and failed-run set -> no duplicate comment.
- Ten prior CI-fix delegations -> publish one limit notice and stop.

### 5. Good/Base/Bad Cases

- Good: an authorized user triggers a current failing PR and receives one
  actionable Codex delegation.
- Base: the current head has no failed run, so the workflow exits without a
  comment.
- Bad: a run has the same SHA but belongs to a push or another PR; it must not
  enter the delegation.

### 6. Tests Required

- The standalone Node self-check covers exact command parsing, both allowlist
  gates, run/PR/head filtering, head changes, idempotency, and push constraints.
- Parse the workflow YAML and run `git diff --check`; run `actionlint` when it
  is available.

### 7. Wrong vs Correct

#### Wrong

Delegate every failed workflow sharing the commit SHA or automatically trigger
Codex whenever CI fails.

#### Correct

Require `/codex-fix-ci`, verify the current PR/head and failed PR runs, then
delegate once with an expected-head guard and a non-force push back to the
original PR branch.

## Scenario: User-triggered Codex Conflict Resolution

### 1. Scope / Trigger

- An authorized user posts the exact `/codex-resolve-conflicts` command on a
  pull request that GitHub currently reports as having content conflicts.
- Conflict detection alone never starts Codex automatically.

### 2. Signatures

- Command: `/codex-resolve-conflicts`
- Event: `issue_comment.created` on a pull request.

### 3. Contracts

- The PR author and command author must both be in `PR_AUTHORS`.
- The PR must remain non-draft with a same-repository head.
- `CODEX_TRIGGER_TOKEN` creates the `@codex` delegation comment.
- The delegation reads the current base tip through the Git refs API, records
  the observed base SHA plus the actual base/head refs and head SHA, and
  requires one ordinary merge commit pushed back to the original head branch.

### 4. Validation & Error Matrix

- `mergeable=null` or `mergeable_state=unknown` -> retry with finite backoff;
  if still unknown, stop.
- `blocked`, `behind`, `clean`, or `unstable` -> not a content conflict; stop.
- Head ref/SHA, base ref, draft status, repository ownership, or conflict
  status changes during inspection -> stop. A base SHA advance on the same base
  ref is normal and must not stop delegation.
- Existing marker for the same observed live base SHA/head -> no duplicate
  delegation; a later live base tip permits a new round.
- Ten prior delegations -> publish one limit notice and stop.

### 5. Good/Base/Bad Cases

- Good: GitHub reports `dirty`; Codex receives the actual base/head contract.
- Base: the PR is merely behind its base; no conflict delegation is created.
- Bad: mergeability remains unknown or turns clean between reads; stop rather
  than asking Codex to mutate the branch.

### 6. Tests Required

- The Node self-check covers exact commands, both allowlists, mergeability
  retries and states, fork/draft transitions, historical versus live base SHA,
  base/head races, idempotency, limits, and Git push constraints.
- Parse workflow YAML and run `git diff --check`; run `actionlint` when
  available.

### 7. Wrong vs Correct

#### Wrong

Treat `behind`, failing checks, or unknown mergeability as a conflict, rebase
the shared branch, or force-push a resolution.

#### Correct

Require explicit `dirty`/non-mergeable evidence, fetch and merge the latest
`origin/<base.ref>` into the protected current head, resolve only the conflicts,
create one merge commit, and ordinary-push only while the remote head still
matches the recorded SHA. Base advancement on the same ref does not block the
push.

Before persistence integration tests, CI applies
`docs/docker/mysql/data_agent.sql` through the root account. MySQL entrypoint
bootstrap scripts run only for an empty volume. Reapplying the script can create
missing objects but cannot upgrade an incompatible memory schema; developers
must use a disposable volume or separately approved exact-target reprovisioning.
Meta tables must never be included in that destructive scope.

For DW/CDC changes, also apply or verify `data_sync.sql` and `source_demo.sql`
in a disposable environment, then run:

```powershell
uv run pytest tests/unit/data_sync
uv run pytest tests/integration/data_sync
docker compose -f docs/docker/docker-compose.yml config
```

For answer-readiness changes, run deterministic classifier/tool/service tests
and the live read-only task-state check:

```powershell
uv run pytest tests/unit/answer_readiness
uv run pytest tests/integration/answer_readiness
```

The replica account stays read-only; integration fixtures mutate `source_demo`
through the local application account. Never recreate or delete the developer's
shared volume merely to rerun entrypoint scripts.

No CI test contacts a live LLM. The LLM infrastructure test mocks the
capability probe; the real worker startup probe is a separate deployment check.

`pyproject.toml` persists Ruff, Pyright, and pytest configuration. Ruff uses
the Google pydocstyle convention, pytest collects from `tests/` with async
support, and the installed `data_agent` package is the runtime import target.

## Configuration Loading

`conf/` is outside `src/`, so uv_build does not ship it inside the wheel. A path
derived from `__file__` therefore cannot locate configuration once the package is
installed, and the console script would fail to start. `resolve_config_path()`
resolves in a fixed order and stops at the first hit:

1. an explicit `path` argument,
2. the `DATA_AGENT_CONFIG` environment variable,
3. `Path.cwd() / "conf/app_config.yaml"`,
4. the source-tree location (development fallback).

An explicit argument or environment variable that points at a missing file is a
hard failure — never fall back. Silently ignoring an explicit choice produces a
process running old configuration while the operator believes it changed, which
is far harder to diagnose than a failed start. When every candidate is missing,
the error lists the absolute paths actually searched and names the environment
variable; a bare `FileNotFoundError` carrying one relative name is not acceptable.

`get_settings()` is the supported accessor and caches per process;
`reset_settings()` exists so tests can load an alternative file. `app_config`
remains a module-level `AppSettings` constant: turning it into a module
`__getattr__` would degrade its inferred type to `Any` and silently remove static
checking from every configuration access in the repository. Note that
`app_config` is still evaluated at import time — making configuration genuinely
lazy additionally requires removing import-time evaluation points (table
`schema=`, Pydantic `max_length=`, arq class attributes, logging default
arguments), which is separate work.

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
- Parser concurrency tests use thread synchronization events to prove SQLGlot
  runs off the event-loop thread and the loop progresses while parsing is in
  flight. Fixed sleeps and polling are not acceptable evidence.
- For graph/worker work, prove interrupt/resume revision safety and that a
  persistence retry does not repeat completed model calls.
- For DW/CDC work, prove safe DDL idempotency, composite-PK keyset continuation,
  ROW INSERT/UPDATE/DELETE convergence, captured/applied coordinate separation,
  cross-source collision without overwrite, lease retry/dead state, and
  bootstrap/Core schema parity.
- For answer readiness, prove one intent repair, catalog enforcement, no-wait
  database bypass, source-scoped versus all-source semantics, `streaming`-only
  readiness, bounded tool output, and zero task mutation.
- Verify source credentials and business row images do not enter API contracts,
  LLM/Redis state, logs, or test output; the replication account must not receive
  source DDL/DML privileges.
- For SSE/Redis Stream work, prove framing, initial authoritative snapshots,
  reconnect cursor behavior, waiting-input continuation, terminal closure,
  safe post-response errors, disconnect cleanup, TTL, and approximate
  over-threshold trimming. Merely asserting that a short Stream is below its
  configured maximum is not evidence that trimming works.
- For persistence/memory work, prove scoped cleanup, rollback, exact compatible
  reuse, soft-delete exclusion, update events, and outbox replay.
- For conversation work, prove text-only contract rejection, stable keyset
  history, tenant isolation, one active turn, turn/outbox idempotency, exact
  quote evidence, ambiguous-confirmation rejection, summary cursor
  monotonicity, and delete-before-purge ordering.
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
