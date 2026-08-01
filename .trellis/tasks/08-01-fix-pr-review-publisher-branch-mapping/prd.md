# 修复 PR 审查发布器的分支映射

## Goal

修复 PR #76 的结构化审查回复发布流程，使本地 checkout/task 分支与 PR 实际 head 分支正确映射，三条指定审查线程都能通过结构化 publisher 处理。

## Confirmed Facts

- 三条指定审查线程已经通过结构化 publisher 尝试处理。
- publisher 对本地分支 `work` 返回 `no pull requests found for branch "work"`。
- 处理必须继续使用 publisher-mediated actions；不得绕过 publisher，也不得直接回复或解决线程。
- PR base 已确认为 `master`；本任务从当前仓库 `master` HEAD 开始。

## Requirements

1. 调查并记录 PR #76 的真实 head 分支，以及本地 checkout/task 分支与该 head 的正确映射关系。
2. 修正导致 publisher 使用本地分支名 `work` 查找 PR 的 workflow、tooling 或 invocation，使其使用正确的 PR head 映射。
3. 保持三条审查线程均由结构化 publisher 处理，不增加直接回复、直接 resolve 或绕过 publisher 的路径。
4. 为分支映射失败增加可重复的验证，覆盖成功查找到 PR 而非再次出现 `no pull requests found for branch`。

## Acceptance Criteria

- [x] PR #76 的 base/head 分支关系有仓库或远端证据支持，且本地 task metadata 不再把临时分支 `work` 当作 publisher 查询 head。
- [x] 相关 workflow/tooling/invocation 已更新为正确映射，并保持 publisher-mediated actions。
- [x] 三条指定审查线程均可通过结构化 publisher 进入处理流程；验证输出不含 `no pull requests found for branch "work"`。
- [x] 未直接回复或解决审查线程，未绕过结构化 publisher。
- [x] 相关测试、脚本检查或等价的端到端 dry-run 验证通过。

## Verification Evidence

- `gh pr view 76` confirms base `master`, head `refactor/separate-frontend-backend-20260801`, and head SHA `8f62fd23a2c0e72312fd49b8ca5332d5477bcb24`.
- The publisher self-check and delegation self-check pass.
- Three publisher-mediated checks using `--pr-number 76` returned `skipped_resolved`; none returned the former branch lookup error.

## Out of Scope

- 不修改 PR #76 的 base/head 以外的协作策略。
- 不直接调用 GitHub API 回复或 resolve 审查线程。
- 不在本任务中实现与分支映射无关的 publisher 功能。

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
