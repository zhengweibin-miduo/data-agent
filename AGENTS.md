<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

# Project Agent Rules

## Review guidelines

Codex GitHub Review、Trellis 检查代理及其他 AI 代码审查必须读取并遵循根目录的 [`code_review.md`](./code_review.md)。该文件是本仓库 AI 代码审查意见格式的唯一规范来源。

## Git 与 Pull Request 操作

凡涉及 Git 状态或历史检查、分支、暂存、提交、推送、变基、拣选以及 Pull Request 创建或维护的任务，必须先读取并遵守项目级 `git-pr-rules` Skill：`.agents/skills/git-pr-rules/SKILL.md`。

项目级 `git-pr-rules` 的授权、分支命名、base/head 和历史安全规则优先于外部工作流或插件 Skill（包括其默认分支前缀）；首次推送前必须校验实际分支名。

Trellis 已规定任务阶段、提交时机或收尾顺序时，以 `.trellis/workflow.md` 为项目工作流来源；Skill 提供 Git 与 PR 操作的安全边界和通用执行规则。

## 技术分析方案文档组合技能

仅当任务需要产出技术分析方案文档时，使用项目级 `create-implementation-plan` Skill：`.agents/skills/create-implementation-plan/SKILL.md`。

若该文档需要架构图、流程图、时序图或其他专业 SVG 可视化，再组合使用项目级 `baoyu-diagram` Skill：`.agents/skills/baoyu-diagram/SKILL.md`。普通实现计划、任务拆解或未要求技术分析方案文档的开发任务不得触发该组合。

## 前端组合技能

仅当任务涉及前端页面或组件的新建、视觉设计、界面重塑或实现审查时，必须按以下顺序组合使用项目级 Skill：

1. **设计**：新建前端页面或组件、开展视觉设计或重塑现有界面前，必须先读取并使用 `frontend-design` Skill：`.agents/skills/frontend-design/SKILL.md`。
2. **实现**：按照 `frontend-design` 确定的设计方向完成前端代码。
3. **审查**：前端实现完成后，必须读取并使用 `web-design-guidelines` Skill：`.agents/skills/web-design-guidelines/SKILL.md`，审查可访问性、UX、性能和 Web 界面最佳实践。

审查发现问题后必须完成修复，并再次使用 `web-design-guidelines` 复查，直至相关问题解决。

## 完整项目知识地图组合技能

仅当任务需要产出“完整项目知识地图”时，按以下顺序组合使用项目级 Skill；该组合与既有“技术分析方案文档组合技能”和“前端组合技能”协同，不替换或重复其规则：

1. **代码库勘察**：先使用 `codebase-onboarding` Skill（`.agents/skills/codebase-onboarding/SKILL.md`）的 Phase 1（Reconnaissance）与 Phase 2（Architecture Mapping），仅基于真实仓库证据梳理结构、入口、模块职责、请求流与数据流；不得执行后续阶段、生成 onboarding artifacts 或创建、修改 `CLAUDE.md`。
2. **领域统一**：使用 `domain-modeling` Skill，统一项目领域术语与概念关系。
3. **边界梳理**：使用 `codebase-design` Skill，明确模块职责、依赖方向与边界。
4. **图示生成**：使用 `baoyu-diagram` Skill，生成专业 SVG 架构图、流程图或时序图。
5. **知识网站呈现**：使用 `web-design-engineer` Skill，实现知识地图的网站视觉呈现（该技能是否已安装由项目现状决定，本规则不扩大安装范围）。
