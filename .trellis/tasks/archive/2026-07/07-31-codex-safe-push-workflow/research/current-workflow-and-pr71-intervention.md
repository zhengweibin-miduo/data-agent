# 当前工作流与 PR #71 干预证据

## 当前停止点

- `.trellis/workflow.md:734` 的 Phase 3.4 负责提交。
- `.trellis/workflow.md:760-776` 固定要求展示 commit plan、再次获得确认，
  且确认后只 commit。
- `.trellis/workflow.md:782` 明确禁止该阶段 push。
- `.agents/skills/git-pr-rules/SKILL.md:39-60` 定义 commit / push 授权边界。
- `.agents/skills/git-pr-rules/SKILL.md:173-203` 要求 fetch，但禁止擅自用
  merge / rebase / reset 同步共享分支。
- `.agents/skills/git-pr-rules/SKILL.md:258-266` 只描述正常 push，没有远端
  干预后的判定分支。

## GitHub 委派入口

- `.github/scripts/codex-review-delegation.js:28-35` 要求远端不等于
  Expected head 时停止，但随后又要求直接 push 原 PR 分支。
- `.github/scripts/codex-ci-fix-delegation.js:56` 同样把 head 变化定义为
  无条件停止。
- `.github/scripts/codex-conflict-resolution-delegation.js:72-74` 在开始和
  push 前都要求 head 严格等于 Expected head。
- 三个脚本都禁止 force-push；这一边界应保留。

Action 准备委派时重新读取 PR 并拒绝陈旧快照是合理的：它避免对已经变化
的问题创建过期任务。需要改变的是委派任务开始运行后，如何处理另一个任务
在线性历史上先完成推送。

## PR #71 观察

PR #71 的 head 分支存在本地/人工提交与 `codex-cloud[bot]` 提交交错。
最近观察到远端从 `b53bf3a` 线性前进到 `3c99c61`；`b53bf3a` 是后者祖先，
因此可用 fast-forward 安全同步，并不需要改写历史。当前任务 worktree 已
按祖先检查和 `merge --ff-only` 同步到 `3c99c61`。

这说明“head 改变”本身不是充分的停止条件；真正的停止条件应是无法证明
线性关系、存在未识别本地工作、发生冲突或需要改写共享历史。
