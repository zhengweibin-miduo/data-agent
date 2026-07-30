# Host-Native Worktree Capabilities

## Core Boundary

Trellis project Python code can identify a platform, inspect Git state, and
emit a structured action directive. It cannot directly invoke model-only host
tools or register a locally created Git worktree as host-managed.

Therefore task creation is a two-runtime protocol:

1. The local Trellis planner selects a host adapter and emits the required host
   action.
2. The main AI session invokes its host-native tool.
3. The host creates/switches to the managed worktree.
4. Trellis verifies the new checkout and creates task metadata there.

## Codex

The current Codex host exposes model tools for thread orchestration:

- `fork_thread` can fork the current task into a Codex-managed worktree.
- `create_thread` can create a separate user-owned task in a saved project
  worktree when the user explicitly requests a new task.
- thread setup is asynchronous and must be followed through the thread
  coordination tools before task bootstrap continues.

These are host tools, not shell commands or Python imports. Project files can
instruct the Codex main agent to call them but cannot call them themselves.

The tracked `.codex/hooks.json` only runs the workflow-state injection hook.
The hook emits context and cannot switch threads or directories.

## Claude Code

Claude Code publicly exposes:

- the main-session `EnterWorktree` tool, which creates an isolated Git
  worktree and switches the session into it;
- `ExitWorktree` for returning to the original directory;
- `claude --worktree` / `-w` for starting a new isolated CLI session;
- `WorktreeCreate` and `WorktreeRemove` hooks for customized lifecycle
  implementations;
- `isolation: worktree` for subagents.

`EnterWorktree` is not available to subagents, so Trellis task creation must
remain a main-session operation.

Sources:

- https://code.claude.com/docs/en/worktrees
- https://code.claude.com/docs/en/tools-reference
- https://code.claude.com/docs/en/hooks

## Other Platforms

The current repository contains identity/session and subagent integration for
several platforms, but no verified host-native worktree creation interface for
Cursor, OpenCode, Gemini, Qoder, CodeBuddy, Copilot, Droid, Pi, Trae, Reasonix,
or ZCode.

Directory presence, hooks, and subagent support are not evidence of a managed
worktree lifecycle interface. In the first version these platforms explicitly
select the existing Trellis-managed `git worktree add` workflow. They are not
reported as host-managed.

## Adapter Result Contract

The platform policy returns one of:

- `delegate`: the main agent must invoke a named host tool with structured
  arguments;
- `ready`: the host has switched the session into a verified linked worktree;
- `trellis_managed`: Trellis must execute its existing worktree creation and
  lifecycle flow;
- `invalid_context`: the action was attempted from a subagent or without the
  required host/session context.

The local planner never reports `ready` until Git confirms that the current
repository root is a linked worktree and differs from the primary checkout.
