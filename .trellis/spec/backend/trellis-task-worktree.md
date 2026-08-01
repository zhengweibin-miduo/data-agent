# Trellis Task Worktree Ownership

## Scenario: Create a Trellis task on any supported Agent platform

### 1. Scope / Trigger

- Trigger: a user approves creation of a new Trellis task while no task is
  active.
- Every supported Agent platform uses the same Trellis-managed Git worktree
  lifecycle.
- Task-creation approval starts planning; it does not authorize implementation.

### 2. Signatures

Worktree and task bootstrap:

```text
git worktree add -b "<task-branch>" "<trellis-worktree-path>" "<start-point>"
python ./.trellis/scripts/task.py create "<title>" \
  --slug <slug> \
  --base-branch <pr-base>
```

Child task bootstrap adds the parent argument:

```text
python ./.trellis/scripts/task.py create "<title>" \
  --slug <slug> \
  --parent <parent-dir> \
  --base-branch <pr-base>
```

### 3. Contracts

- The main session verifies the workspace, PR base, start point, branch name,
  target path, and Git worktree registry before creating anything.
- Trellis creates `.trellis/worktrees/<MM-DD-task-slug>` with
  `git worktree add`; task metadata is created only from inside that worktree.
- `task.py create` requires the reviewed PR base and records the actual current
  branch, canonical worktree root, `meta.worktree_owner=trellis`, and
  `meta.task_creation_policy=trellis_managed`.
- A detached checkout is rejected before task files are written.
- Every child starting point must already contain the parent task directory and
  a readable JSON-object `task.json`; when present, `children` must be a list.
  Missing or invalid parent metadata fails before child files are written.
- Recreating an existing task preserves unknown top-level and `meta` fields
  while refreshing the authoritative creation metadata.
- Historical `worktree_owner=codex` and
  `task_creation_policy=codex_host_managed` values remain readable historical
  facts. Trellis does not migrate them during list, current, or validation, and
  preserves both values when an archive transition updates other task fields.
- The retired task-creation CLI option `--platform` is rejected by argument
  parsing. It must never silently degrade to Trellis ownership.

### 4. Validation & Error Matrix

| Condition | Result |
| --- | --- |
| Reviewed base is absent | Reject before creating task files |
| Checkout is detached | Reject before creating task files |
| Branch, path, or registry target conflicts | Stop before `git worktree add` |
| Worktree preflight succeeds | Create the Trellis worktree and task metadata |
| Parent `task.json` is absent, malformed, not an object, or has invalid `children` | Reject before creating child files |
| Parent metadata exists | Write the bidirectional parent/child link |
| Legacy `--platform codex` is supplied | Argparse rejects it before `cmd_create` |
| Historical Codex owner/policy is read | Accept without migration |

### 5. Good/Base/Bad Cases

- Good: the main session creates a rule-compliant Trellis worktree, switches all
  later tool calls to that root, initializes the developer, and runs the shared
  task bootstrap command with an explicit PR base.
- Base: Codex and non-Codex platforms follow the same Phase 1.0 sequence and
  produce identical Trellis ownership metadata.
- Bad: an Agent asks its host to create a task worktree, passes the retired
  `--platform codex` selector, or creates child files before proving the parent
  metadata exists.

### 6. Tests Required

- Assert filtered Phase 1.0 text for Codex and a non-Codex platform contains
  `git worktree add` and excludes host task delegation terms.
- Assert `task.py create --help` requires a reviewed base and exposes no
  task-creation `--platform` option.
- Assert the retired `--platform codex` command fails before task files exist.
- Assert creation in a disposable linked worktree records actual branch, base,
  canonical root, and Trellis owner/policy.
- Assert duplicate creation preserves unknown top-level and `meta` fields.
- Assert detached HEAD and missing or unreadable parent metadata fail before
  task files are written.
- Assert an existing parent receives the child link and the child records its
  parent.
- Assert a historical Codex-owned task remains readable without mutation.

### 7. Wrong vs Correct

#### Wrong

Route Codex through a host worktree API or accept a platform selector that
changes task ownership.

#### Correct

Use the single Trellis Phase 1.0 lifecycle on every platform. Verify Git targets,
create the branch and worktree with `git worktree add`, then run the shared task
bootstrap command inside it with the reviewed PR base.
