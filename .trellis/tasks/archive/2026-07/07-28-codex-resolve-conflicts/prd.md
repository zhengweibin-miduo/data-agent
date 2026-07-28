# 用户触发 Codex 解决 PR 冲突

## Goal

允许授权用户在 PR 评论中显式委派 Codex 解决当前 PR 与其 base 分支的内容冲突，不依赖网页编辑器，同时保护协作者的新提交和共享分支历史。

## Requirements

- 使用精确评论命令 `/codex-resolve-conflicts` 触发；冲突出现时不得自动触发。
- PR 作者和命令触发者都必须在现有 `PR_AUTHORS` 白名单中。
- 仅处理非 draft、head 位于本仓库且 GitHub 明确判定存在内容冲突的 PR。
- `mergeable` 尚未计算时进行有限退避重试；最终仍为 unknown 时停止，不得委派。
- `blocked`、`behind` 或检查失败不得误判为内容冲突。
- 委派前再次读取 PR，确认 head SHA、base SHA 和分支未变化。
- Codex 必须将实际 PR base 分支普通 merge 到原 head 分支，只解决冲突，不顺带修改业务逻辑。
- 冲突解决和验证后只创建一个 merge commit，普通 push 回原 PR 分支；禁止 rebase 和 force-push。
- 推送前必须再次确认远端 head 仍等于触发时 SHA；变化时停止且不得推送。
- 复用 `CODEX_TRIGGER_TOKEN`、默认分支受信任脚本、`PR_AUTHORS`、幂等 marker 和有限轮次模式。

## Acceptance Criteria

- [ ] 授权用户在真实冲突 PR 评论 `/codex-resolve-conflicts` 后收到一条 `@codex` 解决冲突委派。
- [ ] 非冲突、unknown、draft、fork PR、非精确命令和未授权用户均不委派。
- [ ] head/base 在检查期间变化时停止委派。
- [ ] 相同 PR、base SHA 和 head SHA 的重复命令不会重复委派。
- [ ] 委派正文包含真实 base/head、单 merge commit、推送原分支、禁止 rebase/force-push 和 head 保护要求。
- [ ] Node 自检覆盖冲突判断、unknown 重试、双白名单、竞态和幂等路径。

## Out of Scope

- 自动解决所有冲突或在无法判断业务语义时强行选择一侧。
- 修改 PR base、创建新 PR、rebase 或改写共享历史。
- 处理 fork PR。
