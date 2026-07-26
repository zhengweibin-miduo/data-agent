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
