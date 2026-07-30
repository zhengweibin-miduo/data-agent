# Technical Design

## Decision

Implement a platform policy with two task-creation strategies.

- Codex uses a two-runtime host-managed protocol. The Codex main agent calls the
  Codex host's native task creation tool with `environment: worktree`. Codex
  creates and owns the task, thread, checkout, snapshots, and worktree
  lifecycle. The child Codex task bootstraps Trellis inside that checkout.
- Every other Agent platform uses the existing Trellis-managed protocol:
  Trellis validates names and Git state, executes `git worktree add`, switches
  the session working directory, and then creates task metadata.

The platform policy is explicit. A Trellis-created worktree is never labeled
as Codex-managed.

## Control Flow

### 1. Detect Codex at the platform workflow layer

The Codex `UserPromptSubmit` hook already loads the Codex-specific workflow
state. The no-task and Phase 1.0 instructions route a user-approved task
creation request to a Codex-specific skill.

Platform detection for this action is therefore based on the active platform
entry point, not on the mere presence of a `.codex/` directory.

### 2. Ask Codex to create the managed task/worktree

The Codex main agent:

1. Reads the saved-project list and resolves the project matching the current
   repository.
2. Calls the Codex host `create_thread` tool with:
   - the resolved project;
   - `environment: {type: "worktree"}`;
   - a bootstrap prompt containing the approved Trellis task title, slug,
     developer identity, branch convention, PR base, source requirement, and
     the instruction not to create another worktree.
3. Does not request a model override unless the user explicitly specified one.
4. Returns the Codex created-task reference to the user. A ready result may
   expose a thread ID; setup in progress may expose a client thread ID.

`create_thread` is selected instead of a local `git worktree add` because the
user explicitly requested a new task and Codex must own its lifecycle.

### 3. Bootstrap Trellis in the Codex child task

When the child task starts inside the Codex-managed worktree, its bootstrap
prompt requires it to:

1. Verify that `git rev-parse --show-toplevel` is not the primary project
   checkout and that Git registers it as a linked worktree.
2. Read project `AGENTS.md`, `git-pr-rules`, Trellis workflow, and developer
   identity.
3. Create or switch to the repository-rule-compliant task branch inside the
   existing Codex worktree without creating another worktree.
4. Initialize `.trellis/.developer` when needed.
5. Run `task.py create`, set branch/base metadata, and record the actual
   worktree path.
6. Validate the created Trellis task and continue in planning.

## Codex Skill

Add a project skill dedicated to task creation. Its interface is the user
intent:

```text
Create a Trellis task for <requirement>
```

The skill hides:

- saved-project lookup;
- Codex `create_thread` argument construction;
- bootstrap prompt construction;
- ready versus queued setup results;
- created-task handoff formatting;
- the prohibition on local worktree creation.

The skill is main-agent-only. Codex subagents must report
`invalid_context`; they may research or implement an existing Trellis task but
must not create user-owned Codex tasks.

## Runtime Guard

Prompt instructions alone are insufficient. Add a local verification module
used by `task.py create`:

```text
verify_host_worktree(platform="codex", repo_root=<current-root>)
```

When Codex host-managed mode is enabled:

- the current checkout must be a Git linked worktree;
- its canonical path must differ from the primary project checkout;
- Git must list the path in `git worktree list --porcelain`;
- failure returns a diagnostic telling the main Codex task to invoke the
  Codex task-creation skill;
- the guard never runs `git worktree add`, removes a worktree, or guesses from
  directory names.

The child passes or receives an explicit Codex platform marker from its
Codex-specific bootstrap. Existing session/conversation/transcript identity
resolution remains unchanged.

## Task Metadata

`task.py create` records and verifies:

- the actual current branch;
- the actual canonical worktree root;
- the explicit PR base;
- host metadata under `meta`, including `worktree_owner: codex`.

Legacy setters remain available for existing tasks. Writes preserve unknown
task metadata.

## Non-Codex Strategy

The first version exposes no additional host-native adapter. Claude Code,
Cursor, OpenCode, Gemini, and every other Agent tool continue to use the
existing Trellis Phase 1.0 worktree flow.

The policy result is `trellis_managed`; the workflow performs the existing
branch/path/registry checks and `git worktree add`. Task metadata records
`worktree_owner: trellis`. Adding another host-native adapter later requires a
verified host tool and a separate platform route.

## Failure Semantics

- Project cannot be resolved: stop before `create_thread`.
- Codex task setup is queued: return the client task reference; do not pass it
  to tools requiring a ready thread ID.
- Child starts in the primary checkout: `task.py create` fails closed.
- Child branch/path verification fails: stop without creating Trellis metadata.
- A non-Codex platform selects the Trellis-managed flow; Codex-only runtime
  checks must not block it.
- Remote fetch is unavailable: report the condition and use only the
  repository-approved local start-point policy; do not silently rewrite
  history.
- Existing branch/path conflict: stop and report the exact conflict.

## Testing

Local tests cover the Python-verifiable contract:

- primary checkout is rejected in Codex host-managed mode;
- a disposable linked worktree is accepted;
- a path absent from Git's worktree registry is rejected;
- Windows path normalization remains stable;
- actual branch/worktree/base metadata is written;
- non-Codex policy selects the existing Trellis worktree creation path and
  records `worktree_owner: trellis`;
- Trellis-managed worktrees are never accepted as Codex-managed merely because
  their path exists;
- existing active-task platform/session behavior is unchanged.

The Codex host call is verified through the project skill and current-session
tool schema rather than mocked as a Python API.

## Rollback

Rollback restores the previous Codex workflow/skill text and removes the
Codex-specific host-worktree guard. The non-Codex Trellis-managed flow remains
the baseline and rollback does not delete any worktree.

## Out of Scope

- Host-native worktree adapters for Claude Code or other Agent platforms.
- Making Codex host tools callable from local Python.
- Push, PR creation, remote branch deletion, or local branch deletion.
