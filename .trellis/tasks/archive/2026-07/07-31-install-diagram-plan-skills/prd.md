# 安装规划与图表组合技能

## Goal

让仓库内的 AI 助手共享 `create-implementation-plan` 与 `baoyu-diagram`，并在根 `AGENTS.md` 中明确两者按需组合的触发边界。

## Confirmed Facts

- 源技能分别位于 `C:\Users\midoo\.codex\skills\create-implementation-plan` 和 `C:\Users\midoo\.codex\skills\baoyu-diagram`。
- `create-implementation-plan` 当前仅包含 `SKILL.md`。
- `baoyu-diagram` 除 `SKILL.md` 外，还包含 `references/architecture.md`、`references/flowchart.md`、`references/sequence.md`、`references/structural.md` 和 `scripts/main.ts`。
- 项目级共享技能目录为 `.agents/skills/`；根 `AGENTS.md` 中已有 Trellis、Git/PR 与前端组合技能规则，均须保留。

## Requirements

- 将两个源技能的完整目录复制到 `.agents/skills/create-implementation-plan/` 和 `.agents/skills/baoyu-diagram/`，不得只复制入口文件或遗漏现有引用、脚本等配套内容。
- 保持源技能内容不变，不修改机器级源目录。
- 在根 `AGENTS.md` 新增规划与图表组合技能规则：需要产出实现计划时使用 `create-implementation-plan`；该计划确实需要架构图、流程图、时序图等专业 SVG 可视化时，再组合使用 `baoyu-diagram`。
- 不得把所有实现计划强制要求为图表；仅在用户明确要求图表，或可视化能实质提升复杂关系、流程或交互的理解时触发 `baoyu-diagram`。
- 保留 `AGENTS.md` 现有 Trellis block、Git/PR 规则、前端组合技能规则及其他无关内容。
- 改动限于两个项目级技能目录、根 `AGENTS.md` 和本任务的 Trellis 文件。

## Acceptance Criteria

- [x] `.agents/skills/create-implementation-plan/` 与对应源目录文件清单和内容一致。
- [x] `.agents/skills/baoyu-diagram/` 与对应源目录文件清单和内容一致，包含全部 `references/` 与 `scripts/main.ts`。
- [x] 两个项目级 `SKILL.md` 可读取，且技能名称分别保持为 `create-implementation-plan` 和 `baoyu-diagram`。
- [x] 根 `AGENTS.md` 明确“实现计划使用计划技能、必要可视化再组合图表技能”，且没有把所有计划强制图表化。
- [x] 根 `AGENTS.md` 的既有 Trellis、Git/PR 与前端组合技能规则未被覆盖或删除。
- [x] 最终 Git diff 不包含范围外改动，并通过 `git diff --check`。

## Out of Scope

- 本任务不生成任何业务实现计划或 SVG 图表。
- 本任务不改变两个机器级源技能，也不新增依赖或扩展技能能力。
- 本任务不调整其他项目级技能或既有代理工作流。
