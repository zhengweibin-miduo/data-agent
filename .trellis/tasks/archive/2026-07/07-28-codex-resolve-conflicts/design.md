# 用户触发 Codex 解决 PR 冲突：技术设计

## 边界

- 新增 `issue_comment.created` workflow，仅响应 PR 评论中的精确命令 `/codex-resolve-conflicts`。
- 新增独立 Node.js 委派脚本，复用现有用户触发 CI 修复的安全和自检模式。
- workflow 只委派，不检出或执行 PR head 代码。

## 数据流

1. workflow 识别命令并检出默认分支 `.github/scripts`。
2. 脚本读取 PR，验证同仓、非 draft、PR 作者与触发者双白名单。
3. 有限重试 `pulls.get`，等待 GitHub 完成 mergeability 计算。
4. 仅 `mergeable=false` 或 `mergeable_state=dirty` 进入委派；其他状态安全返回。
5. 再次读取 PR，确认 base/head SHA 与分支均未变化。
6. 用 PR、base SHA、head SHA 生成 marker，完成去重和轮次限制。
7. 发布中文 `@codex` 评论，要求合并实际 base、最小解决冲突、验证和普通 push。

## Git 合同

- `git fetch --prune origin`
- 验证 `origin/<head>` 等于 Expected head，验证 `origin/<base>` 等于 Expected base。
- 在原 head 分支执行普通 merge，不 rebase。
- 只解决冲突；无法可靠决定时停止并报告。
- 只创建一个 merge commit。
- 推送前重新校验远端 head；未变化时执行 `git push origin HEAD:<head>`。
- 禁止 force-push，不创建新 PR。

## 兼容与回滚

- `mergeable=null` 或 `mergeable_state=unknown` 最多有限重试；不得把未知视为冲突。
- `blocked`、`behind`、`unstable` 不属于内容冲突。
- 删除新增 workflow 和脚本即可回滚，不影响 CI 或 review triage。
