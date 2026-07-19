# 为 Trellis 任务强制创建独立分支和 worktree

## Goal

让每个新 Trellis 任务在进入规划阶段前就拥有独立的 Git 分支和
worktree，避免多个任务共享工作目录或把改动落到 PR 基准分支。

## Requirements

- 修改 `.trellis/workflow.md` 的 Phase 1.0 `Create task`，把分支与
  worktree 创建设为 `task.py create` 之前的必需步骤。
- 工作分支必须遵循项目级
  `.agents/skills/git-pr-rules/SKILL.md`，仓库没有更具体规则时使用
  `<type>/<short-slug>-<YYYYMMDD>`。
- 分支类型应从任务性质选择 `feature`、`fix`、`hotfix`、`refactor`、
  `docs`、`test` 或 `chore`，不得使用与 PR 规则冲突的平台默认前缀。
- worktree 路径使用
  `.trellis/worktrees/<MM-DD-task-slug>`，其中目录名与 Trellis
  自动生成的任务目录名一致。
- 创建顺序必须是：确认工作区和 PR 基准分支、确定最终分支名、
  创建分支与 worktree、将会话工作目录切换到新 worktree、最后在
  新 worktree 内运行 `task.py create`。
- 创建前必须检查目标分支和 worktree 路径不存在；创建后必须验证
  当前分支、仓库根目录和任务目录均位于预期 worktree。
- 本次只修改工作流说明，不新增或修改 Trellis Python 自动化脚本。

## Acceptance Criteria

- [x] Phase 1.0 明确禁止直接在当前共享工作目录运行 `task.py create`。
- [x] Phase 1.0 给出符合项目 PR 规则的分支命名格式和类型选择要求。
- [x] Phase 1.0 明确 worktree 命名为
      `.trellis/worktrees/<MM-DD-task-slug>`。
- [x] Phase 1.0 的命令顺序保证 Trellis 任务创建发生在新 worktree
      中，而不是先创建任务再执行 `after_create`。
- [x] Phase 1.0 包含创建前冲突检查和创建后的分支、仓库根目录验证。
- [x] 原有任务树、planning 状态和“只运行 create、不提前 start”
      的语义保持不变。

## Out of Scope

- 不实现自动创建或自动清理 worktree 的 Python 脚本。
- 不修改 `task.py`、`task_store.py` 或 lifecycle hook 行为。
- 不创建、推送或维护远端分支和 Pull Request。
