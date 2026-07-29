# 修复 Codex Outdated thread 解析令牌

## Goal

让手动 Codex 审查委派工作流能够使用用户 PAT resolve Outdated thread，并确保 resolve 失败时仍会继续委派其他遗漏的有效 thread。

## Background

- GitHub Actions 运行 `30420679592` 已为默认 `${{ github.token }}` 配置 `pull-requests: write`，但 `resolveReviewThread` 仍返回 `FORBIDDEN: Resource not accessible by integration`。
- `CODEX_TRIGGER_TOKEN` 对应用户维护的 PAT；该 Token 同时负责发布 `@codex` 委派评论。
- 当前 Resolve 步骤失败会跳过后续 Delegate 步骤，导致一个 Outdated thread 阻塞所有遗漏 thread 的处理。

## Requirements

- 手动触发路径的 Resolve 步骤必须使用 `${{ secrets.CODEX_TRIGGER_TOKEN }}`，不得再使用 `${{ github.token }}`。
- `CODEX_TRIGGER_TOKEN` 必须由仓库管理员配置为对目标仓库拥有 `Pull requests: Read and write` 的用户 PAT。
- Resolve Outdated thread 失败时，工作流必须保留清晰的失败步骤信息，但不得阻止后续 `@codex` 委派步骤执行。
- 自动 `pull_request_review.submitted` 路径的行为保持不变。
- 不新增 Secret、脚本入口或依赖。

## Acceptance Criteria

- [x] Resolve Outdated 步骤通过 `CODEX_TRIGGER_TOKEN` 调用 GitHub API。
- [x] Resolve Outdated 步骤失败后，Delegate 步骤仍会执行。
- [x] Node 自检通过。
- [x] 工作流 YAML 可解析；当前环境未安装 `actionlint`。
- [x] `git diff --check` 通过。

## Out of Scope

- 创建、轮换或写入 PAT Secret。
- 修改 Codex thread 的筛选、去重、阻塞回复或 Outdated 判定逻辑。
