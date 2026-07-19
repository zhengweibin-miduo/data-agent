# 当前 worktree 与分支契约

## 已验证事实

- 项目级 `.agents/skills/git-pr-rules/SKILL.md` 规定：仓库没有更具体
  约定时，工作分支使用 `<type>/<short-slug>-<YYYYMMDD>`。
- 当前 Trellis CLI 版本为 `0.6.7`，`task.py` 没有创建 worktree 的
  子命令。
- `task.py create` 会把当前分支写为 `base_branch`，但新任务的
  `branch` 和 `worktree_path` 初始值均为 `null`。
- `after_create` 在任务目录写入完成后运行，并且 hook 失败只警告，
  因此不适合承担“必须先创建 worktree”的门禁。
- `.trellis/scripts/common/safe_commit.py` 已将
  `.trellis/worktrees/` 视为不得纳入提交的本地运行目录。

## 规划结论

- worktree 创建约束应放在 `.trellis/workflow.md` Phase 1.0。
- worktree 使用 `.trellis/worktrees/<MM-DD-task-slug>`，与任务目录
  同名，避免直接使用包含 `/` 的分支名作为目录。
- 必须先创建并进入 worktree，再在其中运行 `task.py create`。
