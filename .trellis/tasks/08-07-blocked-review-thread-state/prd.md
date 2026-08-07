# 阻塞审查意见保持未解决状态

## Goal

保证 Codex 审查任务已经明确回复“无法安全完成”的 review thread，在后续
手动补触发和过期清理中仍保持 unresolved，使需要产品或架构决策的问题不会
被系统误判为已经完成。

## Background

- 结构化发布器的 `blocked` outcome 已承诺只回复原因、不调用
  `resolveReviewThread`。
- 手动补触发扫描器当前先按 `isOutdated` 分类，只有非过期 thread 才检查
  blocked 回复；因此“blocked 后又 outdated”的 thread 会进入自动 resolve
  队列，违反发布器与项目规范的状态契约。
- blocked 回复可能位于第 100 条之后，现有分页能力必须继续生效。

## Requirements

- 任何 unresolved Codex review thread，只要任一分页回复以
  `无法安全完成：` 开头，就必须被分类为 blocked；该分类优先于
  `isOutdated`。
- blocked thread 不得进入自动 resolve 队列，也不得再次进入委派评论。
- 结构化发布器读取到任意既有 blocked 回复后，后续 fixed、no_change 或
  blocked 任务均不得继续回复或 resolve；在 fixed/no_change 真正 resolve 前必须
  再读取一次 thread，缩小并发任务先后完成造成的竞态窗口。
- 未 blocked 的 outdated thread 继续按现有行为自动 resolve。
- active、resolved、非 Codex reviewer、重复委派以及分页语义保持不变。
- 更新 `code_review.md` 与项目质量规范，使“blocked 始终 unresolved，包括
  outdated 和后续任务晚到回复的情况”成为统一、可执行的状态契约。
- 仅修改本任务所需的 GitHub 审查自动化、对应自测和规范，不修复或重新打开
  历史 thread。

## Acceptance Criteria

- [ ] self-test 证明 blocked + outdated thread 不调用 `resolveReviewThread`。
- [ ] self-test 证明普通 outdated thread 仍被 resolve。
- [ ] self-test 证明第 100 条之后的 blocked 回复仍能阻止 resolve 和重新委派。
- [ ] self-test 证明已有 blocked 回复时，后续三种 outcome 均不调用
  `addReply` 或 `resolveThread`。
- [ ] self-test 证明 fixed/no_change 在最终 resolve 前重新读取 thread，并在并发
  blocked 出现时保持 unresolved。
- [ ] 结构化发布器的 fixed、no_change、blocked 行为保持通过。
- [ ] 相关 Node 自测、workflow 静态检查（可用时）、`git diff --check` 通过。
- [ ] 从 `master` 创建一个独立 PR，不包含 PR #85 的查询实现改动。

## Out of Scope

- 不自动 reopen 已经被错误 resolve 的历史 thread。
- 不改变 Codex 如何判断某条审查意见应为 fixed、no_change 或 blocked。
