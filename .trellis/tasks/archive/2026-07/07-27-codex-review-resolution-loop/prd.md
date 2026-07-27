# 补齐 Codex 审查裁决与解决闭环

## Goal

让 GitHub 中的 Codex 审查意见形成可执行闭环：逐条判断、修复或说明、解决 thread，并在后续推送触发自动复审。

## Background

- 仓库已配置 Codex 在每次推送时自动审查，无需每轮手动 `@codex review`。
- 现有 `codex-review-triage.yml` 依赖 Claude，当前用户已不再使用 Claude。
- ChatGPT/Codex 订阅不提供 OpenAI API Key，但 GitHub Codex 支持由用户评论中的 `@codex` 启动 cloud task 并向 PR 分支推送修复。
- 仓库 Actions Secret `CODEX_TRIGGER_TOKEN` 已配置，并指向仅限当前仓库的用户细粒度 PAT。

## Requirements

1. 移除审查闭环对 Claude、Claude token 和 OpenAI API Key 的依赖。
2. 只处理受信任、同仓库、非草稿 PR 中 Codex 新增的未解决 review threads。
3. Action 使用仓库 Secret 中的细粒度用户 PAT，以该用户身份自动发布一次去重的 `@codex` 委派评论。
4. 委派提示要求 Codex 实际读取代码和 PR diff，逐条完成：
   - 需要修复：最小修复、验证、推送、回复依据并 resolve 原 thread。
   - 不需要修复：在原 thread 说明依据并 resolve。
   - 无法安全处理：说明阻塞原因，保留 thread 未解决。
5. 推送依赖仓库的“每次推送时”自动 Codex Review；只有自动审查未触发时才人工 `@codex review`。
6. 同一 review/commit 只允许委派一次，避免评论风暴或递归触发。
7. 只允许仓库内受信任作者的 PR 使用用户 PAT，且 PAT 仅授予该仓库最小 PR 评论权限。

## Acceptance Criteria

- [ ] Workflow 不再引用 Claude action 或 Claude token。
- [ ] Workflow 不引用 `openai/codex-action` 或 `OPENAI_API_KEY`。
- [ ] 发现新的未解决 Codex threads 时，以 PAT 所属用户身份自动发布 `@codex` 委派评论。
- [ ] 委派提示明确要求“修复并 resolve / 说明并 resolve / 阻塞则不 resolve”。
- [ ] 同一 review/commit 的委派具备幂等标记，不会重复评论。
- [ ] fork、非白名单作者、草稿 PR 和非 Codex review 不会使用 PAT。
- [ ] 相关脚本测试、YAML 解析、Ruff、Pyright 和 `git diff --check` 通过。

## Out of Scope

- 自动合并 PR。
- 绕过分支保护或必需检查。
- 由 GitHub Action 自己运行模型或直接生成补丁。
