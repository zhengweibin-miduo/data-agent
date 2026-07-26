# 项目级技能安装约定

- 仓库级共享技能安装到 `.agents/skills/`，不得安装到用户级 Codex 技能目录。
- 既有任务 `.trellis/tasks/07-25-add-project-skills/prd.md` 要求目标目录存在时停止，不得静默覆盖。
- `.agents/skills/improve-codebase-architecture/` 已存在，本任务只验证它，不重新安装。
- `.agents/skills/frontend-design/` 尚不存在，应从 `anthropics/skills` 安装完整目录。
- 安装后应验证两个技能的 `SKILL.md` 非空、名称匹配，并用 Git 状态确认未修改其他技能或依赖文件。
