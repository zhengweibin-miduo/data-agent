# 调整 Codex 代码审查闭环

## Goal

让 Codex 代码审查形成明确的修复与复审闭环，避免问题修复后直接结束流程。

## Background

- `code_review.md` 规定裁决回复正文不得出现 `@codex`，以免裁决动作自身触发重复审查。
- 当前流程不再依赖 Claude，代码审查、问题修复和复审均由 Codex 完成。
- 本次只调整 `.trellis/workflow.md` 的执行流程提示，不修改裁决工作流或审查等级规则。

## Requirements

1. Codex 代码审查提出需要修复的问题后，由 Codex 完成修复。
2. 修复完成并更新 PR 后，在 PR 普通评论中 `@codex` 请求重新审查。
3. 重新审查仍有问题时，重复“修复 → 请求重新审查”，直至没有待修复问题。
4. 原 review thread 的裁决回复继续禁止出现 `@codex`，避免触发无意义循环。
5. 整个闭环不得要求 Claude 参与。

## Acceptance Criteria

- [ ] `.trellis/workflow.md` 的 Codex 执行流程明确包含“审查发现问题 → Codex 修复 → `@codex` 重新审查”的闭环。
- [ ] 文案明确重新审查请求发生在修复完成后，不与裁决回复混用。
- [ ] 流程不包含 Claude 依赖。
- [ ] 相关 workflow 回归测试通过。

## Out of Scope

- 修改 `.github/workflows/codex-review-triage.yml`。
- 修改 `code_review.md` 的裁决分类和审查输出格式。
