# 在 AGENTS.md 中加入前端组合技能规则

## Goal

在根目录 `AGENTS.md` 中定义前端组合技能的强制路由，使 AI 在前端设计、实现和审查阶段稳定使用对应项目级 Skill。

## Background

- 当前 `AGENTS.md` 只定义了代码审查和 Git/PR Skill 路由，尚未定义前端工作流。
- `frontend-design` 已存在于任务分支，用于前端设计和界面重塑。
- 用户已批准将父工作区中尚未提交的 `web-design-guidelines` 安装一并纳入本任务，使任务分支具备完整的组合技能。

## Requirements

- 在 Trellis 托管区块之外新增前端组合技能规则，避免后续 `trellis update` 覆盖。
- 明确前端页面或组件的新建、视觉设计及界面重塑必须先使用 `frontend-design`。
- 明确前端实现完成后必须使用 `web-design-guidelines` 审查可访问性、UX、性能和 Web 界面最佳实践。
- 明确组合顺序为“设计 → 实现 → 审查”，且审查发现问题后应修复并重新检查。
- 将已验证通过的 `web-design-guidelines` Skill 安装到任务分支的 `.agents/skills/web-design-guidelines`。
- 仅修改与前端组合技能路由直接相关的内容。

## Acceptance Criteria

- [x] 根目录 `AGENTS.md` 在 Trellis 托管区块之外包含独立的“前端组合技能”规则。
- [x] 规则准确引用 `frontend-design` 和 `web-design-guidelines` 的项目路径。
- [x] 规则分别说明两个 Skill 的阶段职责和组合顺序。
- [x] `.agents/skills/web-design-guidelines/SKILL.md` 存在并通过 Skill 格式验证。
- [x] 文案不会要求普通非前端任务加载这两个 Skill。
- [x] Markdown 格式检查通过，且改动范围只包含 `AGENTS.md`、`web-design-guidelines` Skill 和本任务的 Trellis 元数据。
