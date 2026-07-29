# 修复冲突委派 base SHA 来源

## Goal

修复 `/codex-resolve-conflicts` 将 PR 历史 `pull.base.sha` 错当成当前远端 base 分支头的问题，使 Codex 能合并实时 base，同时继续严格保护 PR head，且不向待解决冲突的业务 PR 提交本修复。

## Requirements

- 修复只进入独立分支和独立 PR，以 `master` 为目标；不得提交或推送到 PR #58。
- 使用 GitHub refs API 读取 `refs/heads/<base.ref>` 的实时 SHA，不再把 `pull.base.sha` 当作远端 base tip。
- PR head ref/SHA 在委派期间变化时必须停止。
- PR base ref 变化时必须停止；base SHA 正常前进不得阻止委派或 Codex 推送。
- 委派提示要求 Codex fetch 后合并最新 `origin/<base.ref>`，不再要求其等于历史 Expected base。
- 幂等 marker 使用触发时读取的实时 base SHA；base 前进后允许用户重新触发。
- 保留双白名单、同仓非 draft、明确内容冲突、有限重试、轮次上限、普通 push、禁止 rebase/force-push 等现有保护。

## Acceptance Criteria

- [ ] PR 历史 `base.sha` 与实时 base tip 不同时仍能生成有效委派。
- [ ] 委派正文展示实时 observed base SHA，但不把 base SHA 变化作为停止推送条件。
- [ ] head ref/SHA 或 base ref 变化仍安全停止。
- [ ] marker 使用实时 base SHA；相同实时 base/head 去重，base 前进后生成新 marker。
- [ ] Node 自检覆盖历史 base SHA、实时 base SHA、base 前进、head 变化和委派 Git 合同。
- [ ] 不修改或推送 PR #58。

## Out of Scope

- 自动修改 PR #58 或替用户重新触发命令。
- 改变冲突判定、用户白名单或 Git 历史策略。
