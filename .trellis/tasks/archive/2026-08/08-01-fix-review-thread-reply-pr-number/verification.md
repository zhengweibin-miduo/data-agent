# PR #76 审查线程发布器幂等验证

## 验证范围

- 日期：2026-08-01
- checkout 分支：`fix/review-thread-reply-pr-number-20260801`
- checkout HEAD：`fe74cc3d47c04407f16d5471b6dd5c84060d8314`
- GitHub 活动身份：`zhengweibin-miduo`
- 未执行 commit、push、PR 创建、直接 reply mutation 或直接 resolve mutation。

## 前置门禁

`gh auth status` 和 `gh api user --jq .login` 成功，活动身份为
`zhengweibin-miduo`。`gh pr view 76` 返回：

- 状态：`OPEN`，非 draft
- base：`master`（`cc5ff8e478f5bffa06b7572f5485de20ef27d605`）
- head：`refactor/separate-frontend-backend-20260801`
- head SHA：`e1a47f5aa416f13a4bc4809526d5923f04dbccc1`
- head owner：`zhengweibin-miduo`

发布前的只读 GraphQL 查询确认四条 thread 均为 `isResolved: true`，且
`comments.totalCount` 均为 1。

## 本地自检

以下命令均以退出码 0 完成：

```text
node .github/scripts/codex-review-thread-reply.js --self-test
node .github/scripts/codex-review-delegation.js --self-test
```

两项自检均无标准输出或错误输出。

## 结构化发布器结果

每条 thread 均通过当前 checkout 的唯一结构化发布器执行，使用
`--pr-number 76 --outcome no_change --reason "幂等验证确认该线程已解决"`；
未直接调用任何回复或 resolve mutation。

| Thread ID | 退出码 | 发布器状态 |
| --- | ---: | --- |
| `PRRT_kwDOTXnY3c6VnvyG` | 0 | `skipped_resolved` |
| `PRRT_kwDOTXnY3c6VnvyJ` | 0 | `skipped_resolved` |
| `PRRT_kwDOTXnY3c6VnvyL` | 0 | `skipped_resolved` |
| `PRRT_kwDOTXnY3c6VnvyM` | 0 | `skipped_resolved` |

四次调用均未出现“未知参数：--pr-number”或等价参数拒绝。

## 后置核验

发布器检查后再次执行只读 `gh pr view 76` 和 thread GraphQL 查询：

- PR 仍为 `OPEN`、非 draft，base/head 名称与 SHA 均未变化。
- 四条 thread 仍为 `isResolved: true`。
- 四条 thread 的 `comments.totalCount` 仍均为 1，证明未新增回复。

因此本次执行只验证了发布器的 resolved-thread 幂等分支，没有修改 PR #76
业务代码、base/head 或 thread 状态。

## Trellis 独立复核

2026-08-01 的后续独立复核确认本地 checkout 仍为
`fe74cc3d47c04407f16d5471b6dd5c84060d8314`，但 `gh pr view 76` 返回的远端
head SHA 已前进到 `7ad55dc562ef585bd66c3fbf3d90473bcc4209e4`。PR 仍为 `OPEN`、
非 draft，base 仍为 `master` / `cc5ff8e478f5bffa06b7572f5485de20ef27d605`，
head 分支仍为 `refactor/separate-frontend-backend-20260801`，owner 仍为
`zhengweibin-miduo`。

由于远端 head 与首次验证记录不同，复核按 PRD 第 5 条立即停止发布器调用，
没有再次执行 reply 或 resolve 路径。两项 self-test 各独立运行两次，四次均以
退出码 0 完成，且 stdout/stderr 均为 0 字节。复核前后的只读 GraphQL 查询
均显示四条 thread 为 `isResolved: true`、`comments.totalCount: 1`、
`comments.pageInfo.hasNextPage: false`。这与首次验证记录一致，但本节不把未执行
的发布器调用记为新的 `skipped_resolved` 实测结果。
