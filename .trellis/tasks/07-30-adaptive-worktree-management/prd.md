# 清理旧工作树并适配多开发工具

## Goal

按用户确认的直接删除策略清理本仓库旧工作树，并让 Trellis 首版在 Codex 中创建任务时调用 Codex 原生任务/worktree 能力；其他 Agent 工具继续由 Trellis 创建和管理 worktree。

## Background

- 主工作树为 `D:\projiect\data-agent`，本任务工作树为
  `.trellis/worktrees/07-30-adaptive-worktree-management`；两者在任务执行期间必须保留。
- 清理前，除上述两个工作树外，Git 共登记了 20 个旧工作树：
  3 个位于 `.claude/worktrees`，17 个位于 `.trellis/worktrees`。
- 4 个旧工作树目录已不存在并被 Git 标记为 `prunable`。
- `.claude/worktrees/codex-review-triage-1d3282` 包含 27 个已修改文件和
  4 个未跟踪文件。
- `.trellis/worktrees/07-29-meta-semantic-value-index` 与
  `.trellis/worktrees/07-30-ci-reliability` 各包含未跟踪的 Trellis 任务目录。
- 3 个现存且干净的工作树包含尚未进入现有 `origin/master` 引用的提交：
  `07-26-job-contract-cleanups`、`07-26-memory-correctness-defects` 和
  `07-27-memory-index-analyzer`。
- 当前平台会话识别已经集中支持 Codex、Claude Code、Cursor、OpenCode、
  Gemini 等工具，但任务工作树流程仍把路径和创建动作固定为
  `.trellis/worktrees/<MM-DD-task-slug>`。
- Codex 的“自动删除旧工作树”只管理由 Codex 托管的工作树；仅改变目录名称不能把
  Trellis 创建的工作树注册为 Codex 托管工作树。

## Requirements

- R1：清理前必须识别每个旧工作树中的未提交修改、未跟踪文件和独有提交；用户已明确
  选择不创建 stash 或归档并直接强制删除旧工作树。清理不得改写提交历史或删除分支。
- R2：清理范围为除主工作树和本任务工作树之外的全部旧工作树，包括失效的
  `prunable` 登记；清理工作树不自动删除仍有恢复价值的本地分支或保护记录。
- R3：创建任务请求发生在主工作树时，Trellis 必须先识别当前宿主平台；Codex 路由
  到原生 worktree 任务创建能力，其他平台路由到 Trellis 的 `git worktree add`
  流程。
- R4：首版只实现 Codex 宿主管理适配器，由 Codex 主会话调用原生任务/worktree
  工具并由 Codex 负责生命周期、快照和清理；其他 Agent 工具继续执行现有 Trellis
  worktree 创建、验证和清理流程。
- R5：平台识别与策略选择必须集中实现，并保持现有 session、conversation、
  transcript 及环境变量兼容行为；只有 Codex 选择宿主管理策略，其他平台明确选择
  Trellis 管理策略，不得把 Trellis worktree 标记为 Codex 托管。
- R6：任务元数据必须准确记录实际分支、PR base 和实际工作树路径；Windows 绝对路径
  与反斜杠输入必须能够稳定解析。
- R7：更新 Trellis 工作流、Codex Skill、运行时策略检查与自动化测试，使 Codex
  委托、非 Codex 的 Trellis 回退创建、Codex 主工作树拒绝创建以及 Windows 路径
  场景具有可重复验证。
- R8：本次不删除主工作树、本任务工作树或远端分支，不推送、不创建 PR，也不宣称
  Trellis 回退工作树会被 Codex 自动清理。

## Acceptance Criteria

- [x] 所有旧工作树在删除前均有审计结果；用户明确放弃未提交和未跟踪内容的恢复保护。
- [x] 除主工作树和本任务工作树外，`git worktree list` 不再登记其他工作树，也不存在
  `prunable` 残留。
- [x] 清理未删除任何本地分支；按用户指令未创建 stash 或归档。
- [x] 在 Codex 中创建任务会触发 Codex 原生 worktree/任务派生流程，并在宿主管理的
  worktree 中继续 Trellis 任务创建。
- [x] Claude Code 及其他非 Codex 平台继续由 Trellis 创建、验证和管理 worktree。
- [x] 任务元数据中的 `worktree_path` 与实际仓库根一致。
- [x] 自动化测试覆盖 Codex、Claude Code、无 session identity、平台环境变量隔离、
  linked worktree/主工作树判断以及 Windows 路径。
- [x] 工作流说明与实现保持一致，且相关静态检查和测试通过。

## Out of Scope

- 将 Trellis 自建 worktree 伪装或注册为 Codex/Claude 托管 worktree。
- 首版为 Claude Code、Cursor、OpenCode、Gemini 或其他 Agent 工具实现宿主原生
  worktree 适配器。
- 删除本地或远端分支、推送提交或创建 Pull Request。
