# Codex 代码审查规则调研

## 官方能力边界

- Codex GitHub Review 会查找仓库中的 `AGENTS.md`，并遵循其中的 `Review guidelines`。
- 更接近变更文件的 `AGENTS.md` 具有更具体的作用域。
- 一次性关注点可写在 PR 评论中，例如 `@codex review for security regressions`。
- 当前 GitHub 集成只发布 P0/P1 问题，以避免低优先级评论淹没 PR。
- Codex 通用审查工作流支持从 `AGENTS.md` 引用独立的 `code_review.md`，用于复用详细审查规范。

## 模板设计结论

- 审查意见采用“优先级、风险、证据、修复建议”四段式，保证问题可定位、可判断、可执行。
- GitHub inline thread 已经携带文件与行号上下文，因此正文保持短小，但证据仍需说明触发路径或错误行为。
- 修复回复采用“处理状态、修改说明、验证结果、提交信息”四段式，便于审查者确认闭环。
- `部分修复` 和 `不采纳` 必须补充原因、残余风险或后续动作，避免用一句“已处理”掩盖未闭环问题。
- 模板是提示级约束；本任务不引入 GitHub Action 进行机器校验。

## 来源

- [Codex code review in GitHub](https://learn.chatgpt.com/docs/third-party/github)
- [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex manual](https://developers.openai.com/codex/codex-manual.md)
