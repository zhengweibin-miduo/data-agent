# Trellis Task Worktree Ownership

## Scenario: Create a Trellis task through the active Agent platform

### 1. Scope / Trigger

- Trigger: a user approves creation of a new Trellis task while no task is
  active.
- Codex is the only host-native adapter in the first version.
- Every non-Codex platform retains the Trellis-managed Git worktree flow.
- Task-creation approval starts planning; it does not authorize implementation.

### 2. Signatures

Codex child bootstrap:

```text
python ./.trellis/scripts/task.py create "<title>" \
  --slug <slug> \
  --platform codex \
  --base-branch <pr-base>
```

Non-Codex bootstrap:

```text
git worktree add -b "<task-branch>" "<trellis-worktree-path>" "<start-point>"
python ./.trellis/scripts/task.py create "<title>" \
  --slug <slug> \
  --base-branch <pr-base>
```

Codex host action is a model tool call, not a Python interface:

```json
{
  "prompt": "<complete Trellis bootstrap prompt>",
  "target": {
    "type": "project",
    "projectId": "<saved project id>",
    "environment": {
      "type": "worktree",
      "startingState": {
        "type": "branch",
        "branchName": "<existing start point>"
      }
    }
  }
}
```

### 3. Contracts

- The Codex main agent calls `list_projects` before `create_thread` and resolves
  exactly one saved Git project for the current repository.
- The Codex main checkout never runs `git worktree add` or `task.py create` for
  the delegated task.
- The Codex child verifies that its checkout is a registered linked worktree
  before writing Trellis task files.
- `--platform codex` is an explicit ownership marker. Session variables such as
  `CODEX_SESSION_ID` do not select worktree ownership.
- Every platform value other than `codex`, including an absent value, selects
  `trellis_managed`.
- `task.json` records the actual branch, explicit PR base, canonical worktree
  root, `meta.worktree_owner`, and `meta.task_creation_policy`.
- A Trellis-created worktree must never be labeled as Codex-managed.
- A queued Codex task returns `clientThreadId`; it must not be passed to tools
  requiring a ready `threadId`.
- A Codex child task's selected `startingState` must already contain its parent
  task directory and `task.json`; child creation fails before writing files if
  the parent metadata is absent.

### 4. Validation & Error Matrix

| Condition | Result |
| --- | --- |
| Codex marker in the primary checkout | Reject before creating task files |
| Codex marker without `--base-branch` | Reject with the missing-base error |
| Codex marker in a registered linked worktree | Permit metadata creation |
| Claimed linked path absent from Git registry | Reject as unverified |
| Detached checkout | Reject because task branch metadata is unavailable |
| Non-Codex platform | Use the Trellis-managed Phase 1.0 flow |
| Saved Codex project missing or ambiguous | Stop before `create_thread` |
| Codex setup returns `clientThreadId` | Return the queued created-task directive |
| Codex child source omits its parent `task.json` | Reject before creating child files |

### 5. Good/Base/Bad Cases

- Good: the Codex main agent resolves the saved project, asks Codex to create a
  worktree task, and the child creates Trellis metadata after linked-worktree
  verification.
- Base: Claude Code or another platform creates
  `.trellis/worktrees/<MM-DD-task-slug>` through the established Trellis flow
  and records `worktree_owner=trellis`.
- Bad: local Python sees a Codex session variable and runs `git worktree add`
  while claiming Codex ownership.

### 6. Tests Required

- Assert explicit `codex` selects `codex_host_managed`.
- Assert Codex session identity alone still selects `trellis_managed`.
- Assert the primary checkout is rejected before `.trellis/tasks` is written.
- Assert a disposable registered linked worktree is accepted.
- Assert registry mismatch and Windows path mismatches fail closed.
- Assert Codex and non-Codex task metadata records the correct owner, policy,
  branch, base, and worktree root.
- Assert filtered Phase 1.0 text exposes `create_thread` to Codex and
  `git worktree add` to non-Codex platforms.
- Assert the Codex skill preserves the ready and queued created-task directive
  forms.
- Assert a Codex child cannot be created from a state that omits its parent
  metadata.

### 7. Wrong vs Correct

#### Wrong

Use `.codex/` directory presence or `CODEX_SESSION_ID` as permission for local
Python to create a worktree, then label it Codex-managed.

#### Correct

Use the Codex main-agent skill to invoke the host `create_thread` tool. Require
an explicit Codex bootstrap marker and verify the linked worktree through Git
before creating task metadata. Keep every non-Codex platform on the explicit
Trellis-managed path.
