# Backend Quality Guidelines

## Runtime and Dependency Baseline

- Python is constrained to `>=3.13,<3.14` in `backend/pyproject.toml`, and
  `.python-version` pins `3.13`.
- The distribution is installed from `backend/src/`; direct imports use
  top-level owner modules such as `memory`, `conversation`, `data_sync`, and
  `ddl_metadata`, never `data_agent`, a replacement umbrella package, or
  repository-root import accidents.
- Runtime and development dependencies and their resolved versions are managed
  by `uv`, `backend/pyproject.toml`, and `backend/uv.lock`.
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
  `models`; consumers do not cast shared JSON payloads
  independently.
- Before adding or changing a test, identify the agreed public seam and the
  observable behavior under test. Add cases only for requirements, regressions,
  boundary or error behavior, and high-risk paths; do not mechanically test
  every internal function, private method, or implementation branch.
- Develop tests as vertical slices, one behavior at a time, using the
  lowest-cost test layer that proves the behavior. Use an integration test only
  when proving a contract across a real infrastructure boundary.
- Mock only unavoidable external or system boundaries. Do not mock internal
  collaborators or assert their call counts or ordering; reuse existing fakes,
  factories, and fixtures before creating new test setup.
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
- New and changed tests use native pytest assertions and helpers such as
  `assert` and `pytest.raises()` by default. Use `tests.helpers.checks` only
  when a requirement specifically needs uniform, observable `PASS` / `FAIL`
  output. Existing tests that use `check_*` do not need migration solely to
  satisfy this rule.
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
cd backend
uv sync --locked
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m settings
uv run pytest -m "not tei"
uv build
docker compose -f ../docs/docker/docker-compose.yml config
cd ..
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

## Scenario: Manually Delegate Missed Codex Review Threads

### 1. Scope / Trigger

- Use the `Delegate Missed Codex Review Threads` workflow when a Codex
  review exists but its `pull_request_review` event did not create an automatic
  delegation run.

### 2. Signatures

- Event: `workflow_dispatch`.
- Input: `pr_number` (required positive integer).
- Script entry point:
  `delegateManualReview({ github, context, core, prNumber, prAuthors, reviewBots })`.
- Resolver entry point:
  `resolveOutdatedReviewThreads({ github, context, core, prNumber, prAuthors, reviewBots })`.

### 3. Contracts

- The PR must be non-draft, use a same-repository head, and have an author in
  `PR_AUTHORS`.
- The job grants the ephemeral `${{ github.token }}` only `contents: read`.
- `CODEX_TRIGGER_TOKEN` must be a user PAT with `Pull requests: Read and write`;
  the separate `resolveOutdatedReviewThreads` step uses it to resolve outdated
  threads, and the later delegation step uses it to read the PR and create the
  `@codex` comment as the configured user.
- The scanner includes every unresolved thread whose first comment author is
  in `REVIEW_BOTS`, regardless of the originating review or its head SHA.
- Historical `codex-review-loop` and `codex-review-manual` comments prove only
  that a thread was delegated, not that its task completed. A thread that is
  still active and unresolved must be eligible for manual delegation again.
- An unresolved thread with a later reply beginning with
  `无法安全完成：` is terminally blocked and must not be delegated again.
- Blocked classification takes precedence over `isOutdated`; a blocked thread
  remains unresolved and never enters the outdated-thread resolver queue.
- An unresolved Codex thread with `isOutdated=true` is resolved directly and
  never enters the delegation comment only when it has no blocked reply.
- Completion is derived from current thread state: resolved threads are done,
  explicitly blocked threads are terminally excluded, and remaining active
  unresolved threads are unfinished.

### 4. Validation & Error Matrix

- Invalid `pr_number`, draft PR, fork head, or unauthorized PR author -> fail
  without a comment.
- Failure to resolve an outdated thread -> retain the failed resolver step for
  diagnosis, but continue to the delegation step so other missed threads are
  still delegated.
- The automatic `pull_request_review` event skips the resolver step and retains
  its existing PAT-backed delegation behavior.
- No active unresolved Codex thread -> complete without a comment.
- A changed head with active unresolved threads -> permit a new delegation,
  including threads present in historical delegation comments.
- A blocked thread that is also outdated -> keep it unresolved and exclude it
  from both the resolver queue and the delegation comment.
- A thread that gains a blocked reply after scanning but before the resolver
  mutation -> keep it unresolved after a paginated final thread read.
- A thread that becomes blocked, resolved, or outdated after scanning but
  before the manual delegation comment mutation -> exclude it after a
  paginated final thread read; delegate only candidates that remain active.
- A thread that becomes blocked, resolved, or outdated after an automatic
  review-event scan but before its comment mutation -> exclude it after the
  same paginated final read, including when an earlier event is rerun.

### 5. Good/Base/Bad Cases

- Good: active unresolved Codex threads from different review rounds are
  delegated together while outdated threads are resolved, including unfinished
  threads that were delegated previously.
- Base: every unresolved thread has a later `无法安全完成：` reply, so no
  comment is created.
- Bad: scanning only the first 100 replies misses a later blocked reply; thread
  comments must be paginated before delegation.

### 6. Tests Required

- The standalone Node self-check covers outdated-thread resolution,
  blocked-thread exclusion before scanning and immediately before resolution,
  blocked-thread exclusion immediately before both automatic and manual
  comment creation,
  comment pagination, reviewer filtering, resolved thread exclusion,
  unfinished-thread redelegation, invalid PR rejection, and the rule that the
  PAT-backed delegation path never calls `resolveReviewThread`.
- Parse the workflow YAML and verify the resolver uses `CODEX_TRIGGER_TOKEN`
  with `continue-on-error: true`; run `git diff --check` and `actionlint` when
  it is available.

### 7. Wrong vs Correct

#### Wrong

Delegate only the latest review, use the ephemeral `${{ github.token }}` for
`resolveReviewThread`, let a resolver failure block all active threads, treat a
historical delegation as proof of completion, or retry a thread with an
explicit `无法安全完成：` blocker.

#### Correct

Use the user PAT in `CODEX_TRIGGER_TOKEN` to resolve outdated threads first,
while allowing the later step to continue when resolution fails; then use the
same PAT to exclude resolved and explicitly blocked threads, and delegate every
remaining active unresolved thread even if it was delegated previously.

## Scenario: Publish Structured Codex Review Thread Replies

### 1. Scope / Trigger

- A delegated Codex task has classified an active review thread as fixed,
  no-change, or blocked and needs to publish the result.
- Codex must not construct a GitHub reply body or call reply/resolve mutations
  directly.

### 2. Signatures

- CLI:
  `.github/scripts/codex-review-thread-reply.js --pr-number <number> --thread-id <id> --outcome <fixed|no_change|blocked> ...`
- `pr-number` is required when the publisher runs from a checkout whose local
  branch is not the PR head; it makes `gh pr view` resolve the intended PR
  instead of inferring one from the current branch.
- A delegation generated from the default branch can target an older PR head
  whose local publisher predates the generated CLI. Before publishing, run the
  publisher self-check in the actual execution checkout and confirm that it
  accepts the delegated flags. For recovery, use a trusted checkout containing
  the matching default-branch publisher with an explicit `pr-number`; do not
  patch, merge, rebase, or bypass the stale PR-head publisher just to reply.
- Fixed fields: `thread-id`, `outcome`, `reason`, `fix`, `commit-sha`,
  `test-command`, and `test-summary`.
- No-change and blocked fields: `thread-id`, `outcome`, and `reason`.

### 3. Contracts

- Every caller-provided field is non-empty, single-line, and bounded.
- Fixed replies require a 40-character lowercase commit SHA equal to the
  current open PR head returned by `gh pr view`.
- The formatter owns all Markdown and real newline characters; callers never
  provide a body.
- Literal `\n`, pytest progress, warning summaries, tracebacks, site-package
  paths, pytest documentation links, and long log separators are rejected.
- A hidden marker makes publication idempotent. An existing fixed/no-change
  reply may resume resolution without posting a duplicate.
- Resolved threads are skipped. A thread with any existing reply beginning
  with `无法安全完成：` is skipped without another reply or resolve, regardless
  of the new task's outcome. Blocked replies never resolve a thread.
- Every outcome reads the thread again immediately before publishing; fixed and
  no-change outcomes also read it immediately before their final resolve. A
  concurrently published blocked reply prevents both the stale reply and resolve.

### 4. Validation & Error Matrix

- Invalid or missing outcome fields -> fail before any GitHub mutation.
- Fixed SHA differs from the current PR head -> fail without a reply.
- Thread is already resolved -> return `skipped_resolved`.
- Thread already contains a blocked reply -> return `skipped_blocked` without
  calling the reply or resolve mutation.
- Thread becomes blocked after the initial read but before a reply -> return
  `skipped_blocked` without publishing the stale outcome.
- Thread becomes blocked before a fixed/no-change resolve -> return
  `skipped_blocked` and leave it unresolved.
- Matching marker exists on an unresolved fixed/no-change thread -> resolve
  without another reply only after the final thread read confirms it is not
  blocked.
- Reply mutation fails -> propagate the error and never call resolve.
- Resolve fails after a successful reply -> leave the reply as an idempotent
  recovery marker; the next identical invocation retries only resolve.

### 5. Good/Base/Bad Cases

- Good: a fixed result publishes compact Markdown with the verified remote SHA,
  one test command, and one summary, then resolves the thread.
- Base: a blocked result publishes one reason and leaves the thread unresolved.
- Bad: Codex passes raw pytest output or a body containing escaped newlines; the
  CLI rejects it without changing GitHub state.

### 6. Tests Required

- The standalone Node self-check covers argument parsing, three outcomes,
  formatting with real newlines, forbidden content, fixed SHA validation,
  resolved-thread skipping, marker idempotency, reply-before-resolve ordering,
  reply failure, remote-head mismatch, existing blocked replies for all three
  outcomes, paginated blocked replies, a blocked reply racing publication, and
  a blocked reply racing the final fixed/no-change resolve.
- The delegation script self-check proves its prompt requires the CLI for every
  thread outcome and forbids direct reply/resolve calls.
- For a stale-PR recovery, record both the publisher checkout SHA and the target
  PR head SHA. A later head change stops repeated publication attempts until the
  new remote state is reviewed; already-resolved threads remain a read-only
  `skipped_resolved` check.
- Run both Node self-checks and `git diff --check`.

### 7. Wrong vs Correct

#### Wrong

Let Codex interpolate escaped newlines, commit placeholders, or captured pytest
output into a direct `gh api` mutation and resolve the thread regardless of the
reply result.

#### Correct

Pass bounded structured fields to the repository CLI. Let it validate the
current PR and thread, format Markdown, publish once, and resolve only after a
successful fixed/no-change reply.

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
docker compose -f ../docs/docker/docker-compose.yml config
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

`backend/pyproject.toml` persists setuptools package/module declarations plus
Ruff, Pyright, and pytest configuration. Ruff uses
the Google pydocstyle convention, pytest collects from `tests/` with async
support, and the installed top-level packages/modules are the runtime import
targets. Installed-wheel verification must run outside the repository source
path and confirm both console entry points plus standard-library `logging` identity.

## Configuration Loading

`backend/conf/` is outside `backend/src/`, so setuptools does not ship it inside
the wheel. A path
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

- For each new or changed test, identify the public seam and observable
  behavior, confirm the case protects a requirement, regression, boundary or
  error, or high-risk path, and justify the chosen test layer as the cheapest
  one that proves that behavior.
- Reject tests coupled to private methods, internal collaborator calls, or
  internal call counts and ordering. Confirm unavoidable boundary mocks and new
  setup do not duplicate an existing fake, factory, or fixture.
- Trace configuration changes across `backend/conf/app_config.yaml`,
  `backend/src/settings.py`, every consumer, and configuration validation.
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
- Verify pytest collection runs from `backend/`, uses `tests/`, and imports the
  installed top-level packages/modules rather than a repository-root fallback.
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
- Mechanical test coverage for every internal function, private method, branch,
  or collaborator interaction without a required observable behavior.
- Internal collaborator mocks, internal call-count or ordering assertions, and
  integration tests where a lower-cost layer proves the same contract.
- `asyncio.run()` wrappers or executable main guards in pytest modules.
- Missing or placeholder public Docstrings.
- Claims that a skipped or unavailable live-service check passed.
- A `data_agent` Python package, compatibility shim, or top-level `logging.py`
  that shadows the standard library.

For Query correctness changes, fixed-clock tests must cover leap/calendar
rollovers and a DST-observing IANA zone, validator tests must cover DATE,
DATETIME and TIMESTAMP half-open predicates, and Conversation tests must prove
stale claim fencing plus clarification chains beyond ordinary context limits.
Generation manager tests cover bounded pool construction, READ sharing, WRITE
exclusion, checkout timeout, cancellation, release failure and close; semantics
that require MySQL Locking Service must be reported unavailable when the live
service cannot be reached.
