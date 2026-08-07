# Blocked review thread 状态流取证

## 结论

- `.github/scripts/codex-review-delegation.js:205-216` 的手动扫描先按
  `isOutdated` 分类，只有非 outdated 才分页检查 `无法安全完成：`，所以
  blocked + outdated 会进入 resolver。
- `.github/scripts/codex-review-delegation.js:223-233` 对 resolver 输入直接调用
  `resolveReviewThread`；workflow_dispatch 的独立 resolver step 使用该路径。
- `.github/scripts/codex-review-thread-reply.js:300-350` 只按本次 input marker
  幂等；已有 blocked 回复不能阻止较早委派任务稍后以 fixed/no_change resolve。
- workflow concurrency 只串行 workflow run，不能约束已经创建的外部 Codex 任务。

## 最小修复

1. blocked 分类优先于 outdated，blocked 不进入 active/outdated。
2. publisher 在任何 mutation 前识别任意 blocked 回复并 terminal skip。
3. fixed/no_change 最终 resolve 前再次读取 thread，缩小并发 blocked 的竞态。
4. 自测覆盖 blocked+outdated、101 条后 blocker、晚到 fixed/no_change 和二次读取。

## 验证

- `node .github/scripts/codex-review-delegation.js`
- `node .github/scripts/codex-review-thread-reply.js --self-test`
- workflow 静态检查与 `git diff --check`
