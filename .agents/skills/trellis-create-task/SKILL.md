---
name: trellis-create-task
description: "Create a user-approved Trellis task from the Codex main session by delegating checkout creation to Codex create_thread with a project worktree environment. Use only after task-creation consent and before Trellis planning starts."
---

# Create a Trellis Task Through Codex

Use this project-local skill only in the Codex main session after the user has
approved creation of a new Trellis task. The approval creates a planning task;
it is not approval to start implementation.

## Main-Agent-Only Guard

This skill creates a separate user-owned Codex task. If the current instructions
identify this session as a sub-agent, worker, `trellis-research`,
`trellis-implement`, or `trellis-check`, stop and report:

```text
invalid_context: only the Codex main session may create a user-owned Trellis task.
```

Do not call `create_thread`, run `git worktree add`, or bootstrap task metadata
from a sub-agent.

## 1. Resolve the Approved Task Inputs

Before calling a host tool:

1. Read `.agents/skills/git-pr-rules/SKILL.md`.
2. Inspect `git status --short --branch`, `git remote -v`, and
   `git worktree list --porcelain` without modifying the current checkout.
3. Capture the developer with
   `python ./.trellis/scripts/get_developer.py`. Initialize the parent checkout
   first if it is empty.
4. Resolve and retain:
   - the approved user requirement in full;
   - a concise title;
   - a slug without the `MM-DD-` prefix;
   - the final repository-rule-compliant task branch;
   - the confirmed PR base;
   - the verified Git start point.
5. Prefer a branch start point so unrelated uncommitted parent-checkout changes
   are not copied. Use `{type: "working-tree"}` only when the user explicitly
   requires the current working-tree state as the source.

For a child Trellis task, the selected `startingState` must contain the parent
task directory and its `task.json`. Verify this against the exact branch start
point before calling `create_thread`. If the parent metadata exists only as an
uncommitted working-tree change, do not fall back to a branch that omits it:
use the working-tree source only with the user's explicit approval, or obtain
authorization to commit the parent metadata first. Otherwise stop before
creating the child.

Stop on an unresolved base, start point, branch/path conflict, or missing
developer identity. Do not stash, discard, or move unrelated changes.

## 2. Resolve the Saved Codex Project

Call `list_projects` with no arguments. Match the current canonical repository
root to exactly one saved project and require `isGitRepository` to be true.
Retain its `projectId`.

If no saved project or more than one plausible project matches, stop before
`create_thread` and explain the ambiguity. Directory presence is not a project
identity.

## 3. Build the Child Bootstrap Prompt

The prompt passed to the child task must include every item below:

- approved requirement, title, and slug;
- developer identity;
- final task branch and confirmed PR base;
- verified start-point/source requirement;
- the absolute primary project checkout for comparison;
- a statement that Codex already supplied the worktree and the child must never
  run `git worktree add`, create a nested worktree, or delete a worktree;
- instructions to verify that `git rev-parse --show-toplevel` differs from the
  primary checkout and appears in `git worktree list --porcelain`;
- instructions to read `AGENTS.md`, `.agents/skills/git-pr-rules/SKILL.md`,
  `.trellis/workflow.md`, and the developer identity;
- instructions to create or switch to the final task branch inside the existing
  Codex worktree;
- instructions to initialize `.trellis/.developer` with the captured developer
  when needed;
- the exact bootstrap command:

  ```text
  python ./.trellis/scripts/task.py create "<title>" --slug <slug> --platform codex --base-branch "<pr-base>"
  ```

- instructions to validate the new task, verify `task.json` records the actual
  branch/worktree/base and `meta.worktree_owner=codex`, then remain in planning.

For a child Trellis task, also include `--parent <parent-dir>`, require the child
to verify the parent `task.json` exists before running the bootstrap command,
and require the same bidirectional parent/child validation. Do not create a
child locally in the parent task's checkout.

## 4. Ask Codex To Create the Worktree Task

Call `create_thread` with this verified host contract:

```json
{
  "prompt": "<complete bootstrap prompt>",
  "target": {
    "type": "project",
    "projectId": "<saved project id>",
    "environment": {
      "type": "worktree",
      "startingState": {
        "type": "branch",
        "branchName": "<verified start point>"
      }
    }
  }
}
```

For the explicitly approved working-tree source, replace `startingState` with:

```json
{"type": "working-tree"}
```

Do not pass `model` or `thinking` unless the user explicitly requested an
override. Local Python and shell commands cannot substitute for this host call.

## 5. Return the Created Task

The result has two distinct success shapes:

- ready: retain `threadId` and `hostId`;
- setup in progress: retain `clientThreadId`.

Never pass `clientThreadId` to a tool that requires `threadId`. Return exactly
one app directive line and no Markdown fence:

```text
::created-thread{threadId="<threadId>"}
```

or:

```text
::created-thread{clientThreadId="<clientThreadId>"}
```

The child task owns Trellis bootstrap and planning from that point. The current
main checkout must not run `task.py create` for the delegated task.
