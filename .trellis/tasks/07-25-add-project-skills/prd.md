# 添加项目级代码质量技能

## Goal

将 `mattpocock/skills` 仓库中的两个目标技能及其所需依赖安装到当前项目，使其随项目共享，而不是安装到用户级 Codex 技能目录。

## Confirmed Facts

- 目标项目目录为 `.agents/skills/`。
- 技能来源为 `mattpocock/skills`。
- 目标技能为 `improve-codebase-architecture` 和 `code-review`。
- `improve-codebase-architecture` 引用的依赖技能为 `codebase-design`、`grilling` 和 `domain-modeling`，用户要求一并安装。
- 当前项目缺少 `docs/agents/issue-tracker.md`，因此还需安装 `code-review` 条件性引用的 `setup-matt-pocock-skills`。
- 当前项目已经存在其他项目级 Trellis 技能，安装时不得覆盖无关技能。

## Requirements

- 从指定 GitHub 仓库获取六个技能的完整目录内容。
- 安装目录为 `.agents/skills/improve-codebase-architecture/`、`.agents/skills/code-review/`、`.agents/skills/codebase-design/`、`.agents/skills/grilling/`、`.agents/skills/domain-modeling/` 和 `.agents/skills/setup-matt-pocock-skills/`。
- 每个安装目录必须包含可读取的 `SKILL.md`。
- 如果目标目录已存在，必须停止并报告，不得静默覆盖。

## Acceptance Criteria

- [x] 六个目标技能目录均位于当前任务 worktree 的 `.agents/skills/` 下。
- [x] 六个目录均包含非空的 `SKILL.md`。
- [x] 每个 `SKILL.md` 的技能名称与目录名一致。
- [x] Git 状态只包含本任务的 Trellis 元数据和六个新增技能目录，没有无关改动。

## Out of Scope

- 不安装到用户级 `C:\Users\zwb\.codex\skills`。
- 不修改下载技能的业务内容。
- 不安装 `mattpocock/skills` 中未被本任务点名的其他技能。
- 不提交、推送或创建 Pull Request，除非用户另行授权。
