# 禁止 Codex 审查回复暴露内部裁决标签

## Goal

Codex 处理 GitHub 审查 thread 时，只发布面向用户的中文处理结论，不在回复正文中暴露 `[裁决] SHOULD_FIX` 等内部判断标签，并持续委派未解决的新审查轮次，不设置自动处理轮数上限。

## Background

- 当前委派提示已经规定“已修复”“无需修改”和“阻塞”三类对外回复结构，但没有显式禁止内部裁决标签。
- 根目录 `code_review.md` 只禁止回复正文出现 `@codex`，未覆盖内部裁决标签。
- 已观察到 GitHub thread 回复以 `[裁决] SHOULD_FIX` 开头，违反预期的对外回复格式。
- `Delegate Codex Review Resolution` 当前通过 `MAX_AUTOMATED_ROUNDS = 10` 在第 10 轮停止委派并发布人工处理通知。

## Requirements

- 在统一审查规范中明确：内部判断过程可以保留在执行上下文中，但不得写入 GitHub thread 回复或最终任务总结。
- 明确禁止 `[裁决]`、`SHOULD_FIX` 及同类内部分类标签出现在对外正文中。
- 委派提示继续复用现有三类简短回复模板，不新增运行时过滤器或额外抽象。
- 回归自检必须锁定这条提示约束，防止后续模板调整时误删。
- 删除自动委派轮数上限；只保留同一 review 与 head 的幂等去重，避免重复触发同一轮。
- 删除达到轮数上限时的停止通知及相关无效代码和测试。

## Acceptance Criteria

- [x] `code_review.md` 明确禁止对外回复暴露内部裁决标签。
- [x] `delegationBody()` 明确要求 GitHub 回复和最终任务总结不得出现内部裁决标签。
- [x] 脚本自检断言委派正文包含该禁止规则。
- [x] 委派脚本不再包含最大自动轮数、轮数计数或上限通知逻辑。
- [x] 大量历史委派评论存在时，新 review/head 仍能创建一次委派评论。
- [x] `node .github/scripts/codex-review-delegation.js` 执行通过。

## Out of Scope

- 不修改 Codex 审查问题本身的 P0/P1 标题格式。
- 不对历史 GitHub 回复做批量清理。
- 不增加 GitHub 评论发布后的二次文本过滤。
