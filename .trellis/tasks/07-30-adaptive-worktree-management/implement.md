# Implementation Plan

## 1. Load authoritative project context

- Read `.trellis/workflow.md` completely.
- Read `.trellis/config.yaml`, `.trellis/.template-hashes.json`,
  `.trellis/scripts/task.py`, and the relevant modules under
  `.trellis/scripts/common/`.
- Read `.codex/hooks.json`, the Codex workflow injection hook, Codex agent
  definitions, and the project Trellis skills that describe task creation.
- Preserve unrelated generated-file customizations.

## 2. Add the Codex task-creation skill

- Add a main-agent-only skill that handles approved Trellis task creation.
- Require Codex saved-project resolution before host task creation.
- Instruct the Codex agent to call `create_thread` with a project worktree
  environment.
- Build a complete bootstrap prompt containing title, slug, source requirement,
  developer identity, branch/base rules, and the no-nested-worktree invariant.
- Handle ready `threadId` and queued `clientThreadId` results without confusing
  the two.
- Return the Codex created-task reference using the app-supported directive.

## 3. Synchronize Trellis workflow routing

- Replace Phase 1.0's local `git worktree add` contract for Codex with the
  Codex task-creation skill.
- Keep task-creation consent separate from implementation approval.
- Make child bootstrap run `task.py create` only after Codex has supplied the
  worktree.
- Keep the existing Trellis `git worktree add` contract for every non-Codex
  platform, including its branch/path/registry and Windows safety checks.
- Remove or rewrite project instructions that still require Codex to create
  `.trellis/worktrees/...` locally.

## 4. Add the runtime guard and metadata verification

- Add a small Git worktree inspection module with no create/remove operations.
- Detect primary versus linked worktree from Git common/worktree metadata.
- Add the Codex host-managed guard to `task.py create`.
- Select the policy explicitly: `codex_host_managed` for Codex and
  `trellis_managed` for all other platforms.
- Record actual worktree path, branch, PR base, and the matching
  `meta.worktree_owner`.
- Preserve legacy task fields, unknown metadata, and existing setter commands.

## 5. Add deterministic tests

- Use disposable temporary Git repositories and linked worktrees.
- Verify primary-checkout rejection and linked-worktree acceptance.
- Verify registry/path mismatch and Windows-style path handling.
- Verify non-Codex platforms select the existing Trellis-managed creation path
  in a disposable repository.
- Verify `task.py create` writes actual branch/base/worktree/owner metadata.
- Verify the Codex path never calls `git worktree add`; verify the non-Codex
  path calls it only in the disposable test repository.
- Verify existing session/context resolution behavior remains intact.

## 6. Validate

Run focused Trellis checks:

```text
python -m unittest discover .trellis/scripts/tests
python ./.trellis/scripts/task.py validate 07-30-adaptive-worktree-management
python ./.trellis/scripts/get_context.py --mode phase
```

Run safe static checks:

```text
uv run ruff check .trellis/scripts
uv run pyright .trellis/scripts
uv run python -m compileall -q .trellis/scripts
git diff --check
```

Review the Codex skill against the currently exposed `create_thread`,
`list_projects`, and created-task directive contracts.

## 7. Quality and rollback gates

- Run Trellis check with the curated task context.
- Confirm the real repository still has only the main worktree and this task
  worktree.
- Confirm the main worktree remains clean.
- Confirm no branch was pushed or deleted.
- If Codex cannot expose a user-owned worktree task through the host tool,
  stop and report the capability mismatch; do not silently use the non-Codex
  Trellis-managed strategy for Codex.
