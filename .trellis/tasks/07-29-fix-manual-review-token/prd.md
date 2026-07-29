# 修复手动审查 workflow Token 权限

## Goal

修复 `Delegate Missed Codex Review Threads` 手动运行在 resolve Outdated thread 时因 `CODEX_TRIGGER_TOKEN` 权限不足而失败的问题。

## Background

- Run `30419308768` 在 `resolveReviewThread` 返回 `Resource not accessible by personal access token`。
- 失败发生在委派评论创建前，因此既未 resolve Outdated thread，也未补发遗漏 thread。

## Requirements

- job 的默认 `${{ github.token }}` 仅申请 `contents: read` 和 `pull-requests: write`。
- 使用默认 `${{ github.token }}` 的独立步骤 resolve Outdated Codex thread。
- `CODEX_TRIGGER_TOKEN` 继续只用于发布 `@codex` 委派评论，身份和触发语义不变。
- 自动 `pull_request_review.submitted` 路径不增加写权限调用，行为保持不变。
- 手动路径先完成 Outdated resolve，再补发遗漏 thread；resolve 失败时不得发布委派评论。
- 不依赖运行时构造第二个 Octokit 客户端。

## Acceptance Criteria

- [ ] workflow job 声明 `pull-requests: write`。
- [ ] resolve 步骤明确使用 `${{ github.token }}`。
- [ ] 委派步骤明确使用 `${{ secrets.CODEX_TRIGGER_TOKEN }}`。
- [ ] 手动执行时 Outdated thread 被 resolve，遗漏 thread 随后正常委派。
- [ ] 自动 review 事件不执行独立 resolve 步骤。
- [ ] Node 自检、workflow YAML 解析和 `git diff --check` 通过。

## Out of Scope

- 不扩大 `CODEX_TRIGGER_TOKEN` 权限。
- 不改变遗漏、Outdated、明确阻塞或历史委派 thread 的筛选规则。
