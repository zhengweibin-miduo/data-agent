# 删除 GitHub 审查问题修复回复模板

## Goal

移除仓库中未接入自动化流程的 GitHub 审查问题修复回复模板，避免维护无实际消费入口的规范内容。

## Confirmed Facts

- `code_review.md` 是仓库 AI 代码审查格式的唯一规范来源。
- GitHub inline 审查意见模板仍在使用范围内，需要保留。
- “GitHub 审查问题修复回复模板”章节当前没有被 `code-review` Skill、Trellis 检查代理或自动化脚本直接消费。

## Requirements

- 删除 `code_review.md` 中从“GitHub 审查问题修复回复模板”标题开始的完整章节。
- 同步移除 `code_review.md` 和 `AGENTS.md` 中对审查问题修复回复格式的现行规范声明。
- 保留“GitHub 审查意见模板”、P0/P1 优先级及通用审查规则。
- 不修改代码、Skill、Trellis 检查代理或自动化流程。

## Acceptance Criteria

- [x] `code_review.md` 不再包含“GitHub 审查问题修复回复模板”及“已修复”“部分修复”“不采纳”三套回复格式。
- [x] 当前生效的项目规则不再声明存在审查问题修复回复格式。
- [x] `code_review.md` 仍包含 GitHub inline 审查意见模板和无阻塞问题时的固定回复。
- [x] Markdown 格式检查与 Git diff 检查通过，改动范围仅包含任务元数据和目标规范文件。

## Out of Scope

- 不为审查流程新增自动回复 GitHub review thread 的能力。
- 不重构或接入 `code-review` Skill、Trellis 检查代理。
