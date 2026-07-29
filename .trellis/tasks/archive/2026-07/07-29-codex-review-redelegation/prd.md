# 修复 Codex 已委派未完成线程的重新委派

## Goal

让手动 Codex 审查补偿任务依据 review thread 的实际处理结果重新委派，避免“曾经委派过但始终没有完成”的 unresolved thread 被永久跳过。

## Background

- PR #66 中 `src/data_agent/data_sync/service.py:128` 的 thread 已被写入 delegation marker，但仍保持 unresolved 且没有完成回复。
- `workflow_dispatch` 手动任务只按历史 marker 判断是否委派过，因此输出 `This pull request has no missed unresolved Codex threads.`，没有再次委派该 thread。
- 现有扫描已经以 `isResolved` 排除已完成 thread，以“无法安全完成：”回复排除明确阻塞 thread，并单独处理 outdated thread；历史 delegation marker 不能证明任务已经完成。

## Requirements

- 手动委派必须把当前仍 unresolved、非 outdated、由允许的 Codex reviewer 创建且没有“无法安全完成：”阻塞回复的 thread 视为未完成。
- 未完成 thread 即使已经出现在历史 `codex-review-loop` 或 `codex-review-manual` marker 中，也必须允许手动重新委派。
- 已 resolved、outdated、非 Codex reviewer 或明确阻塞的 thread 必须保持现有排除行为。
- `pull_request_review` 自动委派的同 review/head 幂等规则保持不变。
- 复用现有 thread 状态和回复契约，不增加持久化状态、超时机制或新 workflow。

## Acceptance Criteria

- [x] 已有历史 delegation marker、但仍 active unresolved 的 thread 会被 `delegateManualReview` 再次写入新的手动委派评论。
- [x] resolved thread 不会被重新委派。
- [x] 含“无法安全完成：”回复的 unresolved thread 不会被重新委派。
- [x] outdated thread 仍由现有 resolver 流程处理，不进入委派列表。
- [x] 自动 review/head 委派幂等行为不变。
- [x] 脚本自测覆盖重新委派与排除条件并通过，`git diff --check` 通过。

## Out of Scope

- 跟踪 Codex 外部任务的运行中状态或超时。
- 自动周期重试；重新委派仍由现有手动入口触发。
- 修改 thread 的完成/阻塞回复格式。
