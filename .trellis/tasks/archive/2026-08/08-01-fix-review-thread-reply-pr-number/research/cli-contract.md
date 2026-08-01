# CLI 契约与 PR 参数兼容研究

## 调用方盘点

- `.github/scripts/codex-review-delegation.js:54-58` 是唯一的产品脚本调用契约来源：三种 outcome 都要求 `node .github/scripts/codex-review-thread-reply.js --pr-number <PR_NUMBER> --thread-id <THREAD_ID> ...`。`fixed` 另需 `--commit-sha`、`--fix`、`--test-command`、`--test-summary`；`no_change`/`blocked` 只需 `--reason`。
- `.github/scripts/codex-review-delegation.js:366` 的自检只断言上述命令模板；它不执行 publisher。
- `.github/workflows/codex-review-triage.yml:68,91` 只把 `process.env.PR_NUMBER` 传给 delegation（publisher 由被委派 Codex 按模板执行），没有第二套 publisher CLI。
- `code_review.md:60` 和 `.trellis/spec/backend/quality-guidelines.md:269-270` 将 publisher 定义为唯一结构化回复/resolve 入口，并规定 `--pr-number` 为 checkout 无法推断 PR 时的必填参数。

## 当前脚本契约与拒绝点

- `parseArgs` 在 `.github/scripts/codex-review-thread-reply.js:54-81` 只接受“成对的 flag/value”数组；支持精确 flag `--pr-number`（:57），不支持 `--pr-number=76`，缺 flag/value、未知 flag、重复 flag 都会抛错（:67-79）。因此 shell 形式 `--pr-number 76` 本身可被解析。
- `validateInput` 在 :120-145 将 `prNumber` 先交给 `singleLine`；该函数在 :84-109 拒绝非字符串（:91-93）、空值和换行，然后 :122-124 要求正整数正则 `^[1-9][0-9]*$`。CLI 解析出来的 `"76"` 满足该契约。现有产品调用方只生成 shell CLI，不存在把 JSON 数字直接传给模块 API 的证据，因此数字 API 兼容不属于本任务根因。
- `main` 在 `.github/scripts/codex-review-thread-reply.js:610-613` 先 `parseArgs`，再以 `input.prNumber` 构造 `createGhAdapter(runGh, input.prNumber)`；adapter 的 `getCurrentPr` 在约 :219-232 将其拼成 `gh pr view 76 --json ...`。没有 `--pr-number` 时回退 `gh pr view --json ...`。
- 现有 self-test 已覆盖字符串 CLI 解析及 `gh pr view` 参数（:534-567），但没有覆盖数字 `prNumber: 76` 兼容输入，也没有端到端执行 publisher。

## 为什么 `--pr-number 76` 会被报告拒绝

事实：当前任务从 `master` 的 `fe74cc3d47c04407f16d5471b6dd5c84060d8314` 创建；该 merge commit 已包含 `de7aa1f6e183e492ef3a33a8c98bcbef07ed6328`，后者加入 `--pr-number` 映射、正整数校验、adapter 显式 PR 号及 self-test。

`gh pr view 76` 显示 PR #76 的当前 head 是 `refactor/separate-frontend-backend-20260801` / `e1a47f5aa416f13a4bc4809526d5923f04dbccc1`，base 是 `master`。抓取该 head 后核验：其中 `.github/scripts/codex-review-thread-reply.js` 的 `parseArgs` 不包含 `--pr-number`，而默认分支上的 delegation 已生成带 `--pr-number` 的命令。故失败是默认分支调用契约与旧 PR head 发布器版本错配，不是参数空格形式或当前 `master` parser 的缺陷。

四个指定 thread `PRRT_kwDOTXnY3c6VnvyG`、`PRRT_kwDOTXnY3c6VnvyJ`、`PRRT_kwDOTXnY3c6VnvyL`、`PRRT_kwDOTXnY3c6VnvyM` 经 GitHub GraphQL 只读查询均已为 `isResolved: true`，且当前评论列表只有原始 review comment。实施验证必须尊重发布器的 resolved-thread 幂等契约，预期结果应为 `skipped_resolved`，不得为了补回复而绕过发布器或重新打开 thread。

## 最小兼容边界与回归测试

当前 `master` 已具备本次所需的最小 CLI 兼容实现，不再追加数字 API、`--pr-number=76`、别名或直接 GitHub mutation。实施阶段先以当前任务 checkout 运行现有 publisher/delegation self-test，再对四个 resolved thread 逐一执行带 `--pr-number 76` 的结构化发布器幂等检查。若当前 checkout 仍拒绝参数，立即停止并报告实际 HEAD 与脚本路径；不得在旧 PR head 中临时修改或绕过发布器。

现有回归落点已覆盖本次契约：`.github/scripts/codex-review-thread-reply.js` 的 `selfTest()`（:394-608）断言 `parseArgs(["--pr-number", "76", ...])` 和 `gh pr view 76`；`.github/scripts/codex-review-delegation.js` 的 self-test（约 :366）断言三种 outcome 的命令模板携带 `--pr-number <PR_NUMBER>`。本任务不因旧 PR head 的历史快照重复实现相同修复。

## 建议验证命令

```text
node .github/scripts/codex-review-thread-reply.js --self-test
node .github/scripts/codex-review-delegation.js --self-test
```

真实发布命令应在 `gh auth status`、PR 76 状态/head 已核验后逐 thread 执行；四条已 resolved thread 的预期结果为 `skipped_resolved`。不得用直接 GraphQL mutation 替代 publisher。
