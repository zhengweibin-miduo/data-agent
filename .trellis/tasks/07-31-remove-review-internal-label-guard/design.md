# 设计：结构化发布 Codex 审查回复

## 问题

Codex cloud 当前直接拼接 GitHub review thread 回复。委派提示词虽然要求
实际 SHA、简短验证和固定格式，PR #71 仍出现批量字面量 `\n`、空 SHA、
pytest 进度与 warnings summary。提示词不是发布前的可执行门禁。

## 模块与接口

新增深模块 `.github/scripts/codex-review-thread-reply.js`，外部接口只有一个
CLI 命令：

```text
node .github/scripts/codex-review-thread-reply.js \
  --thread-id <GraphQL thread ID> \
  --outcome fixed|no_change|blocked \
  --reason <单行原因> \
  [--fix <单行修复说明>] \
  [--commit-sha <40位SHA>] \
  [--test-command <单行命令>] \
  [--test-summary <单行摘要>]
```

CLI 内部隐藏参数解析、字段校验、PR head 查询、thread 查询、Markdown
生成、幂等判断、reply mutation 和 resolve mutation。

## 状态规则

| outcome | 必填字段 | 发布后状态 |
| --- | --- | --- |
| `fixed` | reason、fix、commit_sha、test_command、test_summary | reply 成功后 resolve |
| `no_change` | reason | reply 成功后 resolve |
| `blocked` | reason | reply 后保持 unresolved |

- thread 已 resolved：直接跳过，不再回复。
- 相同幂等标记已存在：
  - fixed/no_change：不重复回复，只补做 resolve。
  - blocked：不重复回复，也不 resolve。
- reply mutation 失败：不得调用 resolve mutation。

## 校验

- fixed SHA 必须为 40 位小写十六进制，且等于 `gh pr view` 返回的当前
  `headRefOid`。
- 所有用户提供字段必须是单行、非空、在长度上限内。
- 拒绝字面量 `\n`、pytest 百分比进度、`warnings summary`、Traceback、
  `site-packages` 路径、pytest Docs 链接和大段点阵/等号分隔线。
- Markdown 由 formatter 使用真实换行生成，不接受调用方传入正文。

## GitHub 适配器

CLI 通过无 shell 的 `spawnSync("gh", args, ...)` 调用：

1. `gh pr view --json number,url,headRefName,headRefOid`
2. GraphQL `node(id:)` 查询 thread、resolved 状态和最近 comments
3. `addPullRequestReviewThreadReply`
4. `resolveReviewThread`

适配器作为依赖传给发布函数，内置自测使用 fake，不访问 GitHub。

## 边界

- 仓库无法撤销 Codex 自身的 GitHub 凭据，因此不能从权限层绝对禁止它绕过
  CLI；委派提示词和项目规范会明确禁止直接回复。
- 不自动修改历史错误回复。
- 不处理已经 resolved 的过期任务。
- 当前不推送远端。
