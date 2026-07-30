# Worktree Interface Options

> Superseded: the user clarified that Trellis must delegate creation to the
> current host tool instead of reusing or creating worktrees through a local
> allocator. The active decision is recorded in
> `host-native-worktree-capabilities.md` and `design.md`.

## Evidence

- `.trellis/workflow.md` currently requires every new task to create
  `.trellis/worktrees/<MM-DD-task-slug>` before `task.py create`.
- `.trellis/scripts/common/task_store.py` initializes `branch` and
  `worktree_path` as `null`; the workflow repairs branch/base metadata with
  follow-up commands.
- `.trellis/scripts/common/active_task.py` already centralizes platform and
  session detection for Codex, Claude Code, Cursor, OpenCode, Gemini, and other
  supported tools.
- Git itself provides the reliable ownership-independent fact needed by the
  workflow: the current checkout is either the primary worktree or a linked
  worktree. A project script cannot register its own fallback worktree as a
  Codex-managed worktree.

## Compared Interfaces

### Minimal resolver

Expose a small Python interface that resolves the current mode, materializes a
fallback when required, and records task metadata.

- High depth and low caller complexity.
- Requires callers to compose several Python calls correctly.
- Does not provide a stable shell-facing entry point for AI workflow text.

### Extensible allocator and platform adapters

Introduce host capability adapters, allocation plans, materialization handles,
verification, and release policies.

- Supports hypothetical platform-specific lifecycle policies.
- Adds several interfaces before the repository has two genuinely different
  host allocation adapters.
- Platform identity is not a safe substitute for Git linked-worktree evidence.

### Single prepare command

Expose one stable command:

```text
python ./.trellis/scripts/worktree.py prepare ...
```

The command delegates to an internal worktree module and returns a structured
result describing whether it reused the current linked worktree or created a
Trellis fallback.

- Makes the common AI/workflow caller trivial.
- Keeps Git, filesystem, configuration, and Windows path behavior behind one
  seam.
- Leaves room to add adapters later if a platform exposes a real lifecycle
  interface.

## Decision

Use the single prepare command backed by a small internal worktree module.
Detection is based on Git linked-worktree state, not directory names or
platform claims. Platform/session detection may be recorded as metadata but
does not authorize reuse by itself.

The module has two real behaviors behind one interface:

1. Reuse the current linked worktree supplied by Codex, Claude Code, or another
   host.
2. Create a configured Trellis fallback when invoked from the primary
   worktree.

Automatic deletion of Trellis fallback worktrees is not part of this change.
Only host-created worktrees participate in host-managed cleanup.
