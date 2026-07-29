# 增加 Codex 审查手动委派入口

## Goal

当 `pull_request_review` 事件未创建 GitHub Actions run 时，仓库维护者可以从 Actions 页面按 PR 编号补发从未被任何审查委派评论处理的 Codex thread。

## Background

- 现有 `Delegate Codex Review Resolution` 只监听 `pull_request_review.submitted`。
- 现有 `delegateReview` 已负责筛选未解决 thread、生成委派评论，并按 review/head marker 保证幂等。
- 手动入口应复用现有委派逻辑，不创建第二套评论格式或修复流程。

## Requirements

- 在现有 workflow 中增加 `workflow_dispatch`，只要求输入 PR 编号。
- 手动执行时读取 PR 当前 head，并收集该 PR 上所有来自允许 review bot、仍未 resolve 的审查 thread，不受 review 轮次和原 review head 限制。
- 仅把从未出现在受信任 `codex-review-loop` 或 `codex-review-manual` 委派评论 thread 列表中的 thread 视为遗漏；已经被自动或手动委派过的 unresolved thread 不再补发。
- 遗漏判断以实际委派评论中列出的 GraphQL thread ID 为准，不根据 Actions run、review 时间或 head SHA 猜测。
- 若 unresolved thread 已有后续回复以 `无法安全完成：` 开头，视为已明确阻塞，手动执行时排除该 thread。
- GitHub GraphQL 标记为 `isOutdated=true` 的 unresolved Codex thread 不进入委派列表，并由手动 workflow 直接 resolve。
- 委派提示中的阻塞回复使用稳定格式 `无法安全完成：<阻塞原因>`，使后续手动筛选可确定执行。
- 手动路径继续校验 PR 非 Draft、head 来自本仓库、PR 作者在允许名单内。
- 没有未解决的 Codex thread 时正常结束且不创建评论；PR 不符合条件或输入无效时明确失败且不得创建评论。
- 找到 thread 后创建一条 `@codex` 委派评论，只列出从未委派且未明确阻塞的 unresolved thread，并要求 Codex 逐条修复、说明或保持 unresolved。
- 自动 `pull_request_review.submitted` 路径行为保持不变。
- 同一 thread 一旦出现在自动或手动委派评论中，即使 head 后续变化也不得再次补发；仅新发现的遗漏 thread 可进入下一次手动委派。

## Acceptance Criteria

- [ ] Actions 页面可输入 PR 编号执行 `Delegate Missed Codex Review Threads`。
- [ ] 手动入口名称和 PR 编号说明明确表示只委派未被标记为“无法安全完成”的 thread。
- [ ] 合法 PR 存在任意轮次从未委派的未解决 Codex thread 时，创建一条只包含这些遗漏 thread 的 `@codex` 委派评论。
- [ ] 已出现在受信任自动或手动委派评论中的 unresolved thread 不重复进入补发列表。
- [ ] 已有 `无法安全完成：` 回复的 unresolved thread 不出现在手动委派评论中。
- [ ] `Outdated` unresolved Codex thread 不出现在手动委派评论中，并被 workflow resolve。
- [ ] 已经自动或手动委派过的 thread 在 head 变化后仍不重复补发。
- [ ] Draft、fork head、未授权 PR 作者等情况明确失败且不评论。
- [ ] 没有未解决 Codex thread 时正常结束且不评论。
- [ ] 自动 review 事件路径继续工作。
- [ ] 最小自检覆盖手动 thread 筛选、分页、幂等与拒绝路径，现有脚本自检通过。

## Out of Scope

- 不增加新的 workflow 文件。
- 不手动创建或重放 GitHub review 事件。
- 不修改自动委派的 review/thread 归属判断或幂等语义。
