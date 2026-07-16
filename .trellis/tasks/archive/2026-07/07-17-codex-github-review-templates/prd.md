# 规范 Codex GitHub 审查与修复模板

## Goal

为仓库建立一份独立、可复用的 Codex 代码审查规范，使 GitHub Review 的问题描述和审查问题修复回复保持简体中文、证据充分、格式一致并可验证。

## Background

- 根目录 `AGENTS.md` 已要求 AI 代码审查结果使用简体中文，并保留英文代码标识符、路径、命令和错误原文。
- Codex GitHub Review 会读取 `AGENTS.md` 中的 Review guidelines；复杂规则适合放到独立 `code_review.md`，再由 `AGENTS.md` 引用。
- 用户要求创建合并到 `master` 的 Pull Request。

## Requirements

- 新增根目录 `code_review.md`，承载所有 AI 代码审查规范。
- `AGENTS.md` 的 Review guidelines 改为引用 `code_review.md`，避免两处规则重复或漂移。
- `code_review.md` 必须包含：
  - 审查语言、证据、优先级和误报核验规则。
  - GitHub 审查意见的固定 Markdown 模板。
  - 修复审查意见后，在 GitHub 线程中回复的固定 Markdown 模板。
  - `已修复`、`部分修复`、`不采纳` 三种处理状态及各自的信息要求。
  - 未发现问题时的统一回复。
- 模板应简洁，适合直接用于 GitHub inline review thread；代码标识符和证据不得因中文化而失真。
- PR 目标分支必须为 `master`，分支、提交、推送和 PR 范围遵守项目 `git-pr-rules`。

## Out of Scope

- 不新增 GitHub Action 或机器人来强制校验评论格式。
- 不修改业务代码、测试代码或运行时行为。
- 不修改 GitHub 仓库的 Codex Code review 开关。

## Acceptance Criteria

- [x] 根目录存在 `code_review.md`，同时覆盖审查意见和修复回复模板。
- [x] `AGENTS.md` 明确要求 Codex GitHub Review、Trellis 检查代理及其他 AI 审查读取并遵循 `code_review.md`。
- [x] 审查模板至少包含优先级、风险、证据和修复建议。
- [x] 修复回复模板至少包含处理状态、修改说明、验证结果和提交信息；未完全修复时包含原因与后续动作。
- [x] Markdown 链接和格式通过人工/脚本检查，`git diff --check` 通过。
- [x] 改动被提交并推送到独立工作分支，创建 base 为 `master` 的 GitHub PR：[#18](https://github.com/zhengweibin-miduo/data-agent/pull/18)。

## Notes

- 本任务为轻量文档配置任务，采用 PRD-only。
- 固定模板属于提示级约束，不引入机器强制 Schema。
- Spec update 判断：本次没有修改 API、数据、基础设施或跨层契约；新的审查约定已由根目录 `code_review.md` 作为唯一规范来源承载，因此不重复写入 `.trellis/spec/`。
