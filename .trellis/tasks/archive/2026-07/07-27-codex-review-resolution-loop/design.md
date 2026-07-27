# Technical Design

## Boundaries

GitHub Action 只负责发现新意见并代用户发出 Codex cloud task 委派，不读取 OpenAI API、不修改代码、不 resolve thread。

Action 使用细粒度 PAT Secret `CODEX_TRIGGER_TOKEN` 调用 GitHub Issues Comment API。GitHub 将评论作者记录为 PAT 所属用户，Codex GitHub App 因 `@codex` mention 启动订阅内的 cloud task。

## Contracts

委派评论包含：

- 固定的 `@codex` 任务说明；
- review id、head SHA 和未解决 thread id；
- HTML 幂等标记 `<!-- codex-review-loop:<review-id>:<head-sha> -->`。

再次处理同一 review/commit 前先搜索现有 PR 评论；命中标记则跳过。

## Data Flow

`pull_request_review: submitted` → 查询该 review 新增的未解决 Codex threads → 以用户 PAT 发布幂等 `@codex` 委派评论 → Codex cloud task 判断并处理每条 thread → 修复时推送到 PR 分支 → 自动触发下一轮 review。

Codex task 被明确要求：需要修复则修改、验证、回复并 resolve；不需要修复则说明依据并 resolve；无法完成则说明并保持 unresolved。

## Compatibility and Safety

- 保留同仓 PR、作者白名单、Codex reviewer 白名单和单 PR concurrency。
- 仅 `pull_request_review: submitted` 可触发，Action 自己发布的 issue comment 不会递归触发。
- PAT 只授权当前仓库的 Pull requests: Read and write；不得复用当前高权限 `gh` token。
- 缺少 `CODEX_TRIGGER_TOKEN` 时明确失败，不回退 Claude 或机器人身份评论。

## Rollback

回滚本任务提交并删除 `CODEX_TRIGGER_TOKEN` Secret 即可；不涉及数据库或运行时迁移。
