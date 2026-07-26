# 安装架构分析与前端设计技能

## Goal

将会话 `019f9bfd-8e06-74a2-b823-594965672535` 中确定的架构分析与 HTML 页面设计技能安装为当前仓库可共享的项目级技能。

## Confirmed Facts

- 项目级技能目录为 `.agents/skills/`。
- `improve-codebase-architecture` 已由既有任务安装到 `.agents/skills/improve-codebase-architecture/`。
- `frontend-design` 尚未安装，来源为 `anthropics/skills` 仓库。
- 安装不得覆盖或修改现有技能。

## Requirements

- 保留并验证现有 `improve-codebase-architecture` 技能。
- 将 `frontend-design` 的完整技能目录安装到 `.agents/skills/frontend-design/`。
- 两个目标目录都必须包含非空且名称匹配的 `SKILL.md`。
- 若 `.agents/skills/frontend-design/` 在执行安装前已经存在，则停止并报告，不得静默覆盖。

## Acceptance Criteria

- [x] `.agents/skills/improve-codebase-architecture/SKILL.md` 存在、非空，技能名称匹配目录名。
- [x] `.agents/skills/frontend-design/SKILL.md` 存在、非空，技能名称匹配目录名。
- [x] 现有 `improve-codebase-architecture` 及其他项目级技能未被修改。
- [x] Git 状态只包含本任务的 Trellis 元数据和新增的 `frontend-design` 技能目录。

## Out of Scope

- 不安装到用户级 `C:\Users\zwb\.codex\skills`。
- 不修改下载技能的内容。
- 不修改 Python 依赖文件。
- 不提交、推送或创建 Pull Request，除非用户另行授权。
