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

- Codex GitHub Review、Trellis 检查代理及其他 AI 代码审查结果必须使用简体中文。
- 问题标题、风险说明、证据和修复建议使用中文。
- 代码标识符、文件路径、命令、配置键、日志和错误原文保留英文。
- 执行代码审查命令前必须先运行 `uv sync --locked`，安装锁文件中声明的开发依赖。
- 执行 Ruff 时必须使用 `uv run ruff check app app_test`。
- 执行 Pyright 时必须使用 `uv run pyright app app_test`。
- 审查执行阶段不得使用 `uv run --with ...` 临时访问 PyPI；Ruff 和 Pyright 必须来自项目锁定的开发依赖。
- 正式 Pyright 命令通过时，不得将审查环境无法解析 `elasticsearch`、`sqlalchemy`、`qdrant_client`、`pydantic` 等第三方依赖误报为代码问题。

## Codex 审查模板

- 执行只读代码审查时，必须读取并遵循仓库根目录 `code_review.md`。
- 核验并修复审查意见时，必须读取并遵循仓库根目录 `code_review_fix.md`；是否允许暂存、提交、推送、发布 PR 评论、解决审查线程、更新已有 PR 或创建 PR，以模板中填写的“允许操作”和下方 Git 规则为准，各项权限不得相互扩张。

## Git 与 Pull Request 操作

凡涉及 Git 状态或历史检查、分支、暂存、提交、推送、变基、拣选以及 Pull Request 创建或维护的任务，必须先读取并遵守项目级 `git-pr-rules` Skill：`.agents/skills/git-pr-rules/SKILL.md`。

项目级 `git-pr-rules` 的授权、分支命名、base/head 和历史安全规则优先于外部工作流或插件 Skill（包括其默认分支前缀）；首次推送前必须校验实际分支名。

Trellis 已规定任务阶段、提交时机或收尾顺序时，以 `.trellis/workflow.md` 为项目工作流来源；Skill 提供 Git 与 PR 操作的安全边界和通用执行规则。
