# 用户触发 Codex 修复 CI

## Goal

允许授权用户在 PR 评论中显式触发 Codex 处理当前提交的 GitHub Actions CI 失败，无需复制失败日志，同时避免自动循环、越权触发和基于过期提交修复。

## Requirements

- 使用固定评论命令 `/codex-fix-ci` 触发；普通评论和自动 CI 失败不得自动触发。
- PR 作者和命令触发者都必须在现有 `PR_AUTHORS` 白名单中。
- 仅处理非 draft、head 位于本仓库且当前 head SHA 确实存在失败 GitHub Actions 检查的 PR。
- 委派前重新读取 PR head；失败检查必须属于该 PR 的当前 head SHA。
- 委派评论应包含失败 workflow/job 的链接、当前 head SHA、原 PR 分支及明确的最小修复、验证、提交和推送要求。
- 复用 `CODEX_TRIGGER_TOKEN` 创建 `@codex` 评论，workflow 只检出默认分支上的受信任脚本，不执行 PR head 代码。
- 同一 PR、head SHA 和失败 run 集合只能委派一次，并设置有限轮次，防止重复评论或修复循环。
- Codex 的 GitHub 回复和最终总结使用简体中文，只保留提交 SHA、根因、修复内容和测试摘要，不粘贴完整 Actions 或测试日志。

## Acceptance Criteria

- [ ] 授权用户在存在当前-head失败检查的 PR 评论 `/codex-fix-ci` 后，出现一条包含 `@codex` 的修复委派评论。
- [ ] 无失败检查、draft、fork PR、非 PR 评论、非精确命令和未授权用户均不会委派。
- [ ] head 在检查期间变化时停止委派。
- [ ] 重复执行同一命令不会为相同 head 和失败 run 集合创建重复委派。
- [ ] workflow 仅从默认分支加载脚本，并采用最小 GitHub 权限。
- [ ] Node 自检覆盖命令识别、权限门禁、失败 run 筛选、head 变化和幂等去重。

## Out of Scope

- CI 失败后自动触发 Codex。
- 非 GitHub Actions 的外部 CI。
- 修改 Codex Review 审查意见的输出格式。
