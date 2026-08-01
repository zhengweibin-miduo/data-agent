# 修复审查线程回复脚本的 PR 参数兼容

## Goal

消除 PR #76 审查线程回复命令因发布器版本错配而拒绝 `--pr-number` 的问题，并用当前默认分支上的结构化发布器验证四个指定 thread 的幂等处理结果。

## Confirmed Facts

- PR #76 的 base 是 `master`。首次受控验证时 head 为 `refactor/separate-frontend-backend-20260801` / `e1a47f5aa416f13a4bc4809526d5923f04dbccc1`；后续 Trellis 独立复核发现同一 head 分支已前进到 `7ad55dc562ef585bd66c3fbf3d90473bcc4209e4`，因此按停止门禁未再次调用发布器。
- PR #76 head 中的旧 `.github/scripts/codex-review-thread-reply.js` 不支持 `--pr-number`；默认分支 delegation 生成的三种 outcome 命令均携带该参数，构成版本错配。
- 当前任务基线 `fe74cc3d47c04407f16d5471b6dd5c84060d8314` 已包含 `de7aa1f6e183e492ef3a33a8c98bcbef07ed6328` 的兼容修复：publisher 接受 `--pr-number 76`，校验正整数，并执行 `gh pr view 76`。
- 当前 publisher 与 delegation 的 self-test 已分别覆盖参数解析、显式 PR 查询和命令模板契约。
- 四个指定 thread 当前均已 resolved；按发布器契约，重复处理应返回 `skipped_resolved`，不得新增回复或再次 resolve。

## Requirements

1. 以当前任务 checkout 中的 publisher 为唯一结构化回复入口，不在 PR #76 的旧 head 上执行不兼容脚本。
2. 保持现有 CLI 契约：`--pr-number` 使用独立 flag/value 形式并传递正整数字符串；不新增数字模块 API、`--pr-number=76`、别名或直接 GitHub mutation 路径。
3. 运行 publisher 与 delegation 自检，证明当前调用方和脚本契约一致。
4. 在核验 GitHub 身份、PR #76 状态和 head 后，对以下 thread 分别执行一次带 `--pr-number 76` 的结构化发布器检查：
   - `PRRT_kwDOTXnY3c6VnvyG`
   - `PRRT_kwDOTXnY3c6VnvyJ`
   - `PRRT_kwDOTXnY3c6VnvyL`
   - `PRRT_kwDOTXnY3c6VnvyM`
5. 若实际 checkout 不支持 `--pr-number`、PR 状态/head 改变、身份不可用或返回结果不是预期幂等状态，立即停止并报告证据，不得绕过发布器。

## Acceptance Criteria

- [x] `node .github/scripts/codex-review-thread-reply.js --self-test` 通过。
- [x] `node .github/scripts/codex-review-delegation.js --self-test` 通过，三种 outcome 模板继续携带 `--pr-number <PR_NUMBER>`。
- [x] 四个指定 thread 的命令均不再出现“未知参数：--pr-number”或等价参数拒绝。
- [x] 四个已 resolved thread 均由结构化发布器返回 `skipped_resolved`，且不新增回复、不执行重复 resolve。
- [x] 验证记录包含实际 checkout HEAD、PR #76 的 base/head、每个 thread ID 与发布器返回状态。
- [x] 未修改 PR #76 的业务代码、base/head 或 thread 状态，未直接调用回复/resolve mutation。

## Verification Evidence

完整执行命令、实时 GitHub 状态和逐 thread 结果记录于
[`verification.md`](./verification.md)。2026-08-01 的受控验证确认当前 checkout
HEAD 为 `fe74cc3d47c04407f16d5471b6dd5c84060d8314`，PR #76 在验证前后均保持
`master` -> `refactor/separate-frontend-backend-20260801` /
`e1a47f5aa416f13a4bc4809526d5923f04dbccc1`，四条 thread 均返回
`skipped_resolved`，且验证前后均为 resolved、评论数均为 1。

## Out of Scope

- 不重复实现已合入 `master` 的 `de7aa1f` 修复。
- 不为历史 PR head 增加自动同步、merge、rebase、cherry-pick 或临时覆盖发布器的机制。
- 不增加新的 GitHub Action、远程发布服务或通用跨版本协议。
- 不修改四条 review finding 对应的前端业务代码。

## Planning Classification

轻量任务，PRD-only。研究已证明兼容实现存在于当前基线，剩余工作是受控回归与四线程幂等验证，不需要新的技术设计或多阶段实现计划。
