# 结构化发布 Codex 审查回复

## Goal

删除已经没有生成端或解析端依赖的内部标签防御说明，并为 Codex 审查
thread 提供结构化回复发布器，避免字面量换行、缺少提交 SHA、完整测试日志
和错误 resolve 状态进入 GitHub。

## Requirements

- 从 `code_review.md` 删除禁止 `[裁决]`、`SHOULD_FIX` 等内部标签的条目。
- 从 Codex 审查委派提示词删除同一禁止说明。
- 删除只验证该说明存在的脚本测试断言。
- 新增结构化 CLI，支持 `fixed`、`no_change`、`blocked` 三种 outcome。
- `fixed` 必须提供 40 位 SHA，且必须等于当前 PR 远端 head；回复正文由
  CLI 使用真实 Markdown 换行生成。
- 结构化字段必须拒绝字面量 `\n`、pytest 进度、warnings summary、
  堆栈、完整日志和多行输入。
- CLI 必须先查询 thread：已 resolved 时跳过；相同幂等标记已发布时不得
  重复回复。
- 只有 `fixed`、`no_change` 在回复成功后 resolve；`blocked` 始终保持
  unresolved；发布失败不得 resolve。
- 委派提示词和 `code_review.md` 必须要求通过结构化 CLI 回复，禁止直接
  拼 GitHub comment 或直接调用 reply/resolve mutation。
- 不修改审查优先级或业务修复规则，不清理历史回复和过期回复。
- 当前只完成本地提交；遵守用户“先不推送”的要求。

## Acceptance Criteria

- [x] 有效工作流代码与规范不再包含 `[裁决]` 或 `SHOULD_FIX` 标签约束。
- [x] 结构化 CLI 对三种 outcome 生成固定、紧凑且使用真实换行的正文。
- [x] 缺失/错误 SHA、远端 head 不匹配、字面量换行和完整日志均被拒绝。
- [x] fixed/no_change/blocked、已 resolved、幂等重试和发布失败顺序均有测试。
- [x] Codex 委派脚本内置测试与 `git diff --check` 通过。
- [x] 改动仅保留在本地提交，未推送 PR #71。

## Notes

- 仓库历史任务记录中的旧标签说明属于审计历史，不在本次删除范围内。
- 用户要求暂不推送；本地提交完成后保留任务，等待后续明确恢复推送。
- PR #71 现有错误回复不做批量编辑；本任务只防止未来回复复发。
