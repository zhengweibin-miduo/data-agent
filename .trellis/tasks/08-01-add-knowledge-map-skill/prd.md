# 补充完整项目知识地图技能组合

## Goal

为仓库补充一套可发现、可按顺序组合使用的“完整项目知识地图”能力，使 AI 在生成项目知识地图时先基于真实代码建立结构与数据流认知，再补齐领域术语、模块边界、专业图示和知识网站视觉实现。

## Background

- 当前仓库已经在根目录 `AGENTS.md` 中定义“技术分析方案文档组合技能”和“前端组合技能”，新增规则必须与两者协调，不能覆盖或重复其职责。
- 目标第三方技能为 `affaan-m/ECC` 的 `codebase-onboarding`，计划通过 `npx skills add affaan-m/ECC --skill codebase-onboarding` 按项目级安装。
- 当前基线已存在 `domain-modeling`、`codebase-design` 和 `baoyu-diagram`；当前 `.agents/skills/` 中未发现名为 `web-design-engineer` 的技能目录，但本任务仍按已批准需求在组合规则中保留该名称，不将安装它扩展为本任务范围。

## Requirements

- R1. 在项目范围安装 `codebase-onboarding`，安装结果必须位于仓库内的技能目录，不能写入用户级全局技能目录。
- R2. 根目录 `AGENTS.md` 新增独立的“完整项目知识地图”组合技能规则，并明确建议顺序：
  1. `codebase-onboarding`：读取真实仓库结构、入口、模块职责、请求流与数据流；
  2. `domain-modeling`：统一领域术语；
  3. `codebase-design`：梳理模块边界；
  4. `baoyu-diagram`：生成专业 SVG；
  5. `web-design-engineer`：实现知识网站视觉呈现。
- R3. 新规则必须说明该组合仅在产出“完整项目知识地图”时触发，并与已有技术分析方案文档、前端组合技能规则协同，不替换、不重复它们。
- R4. 保留既有 `AGENTS.md` 内容与现有项目技能，不覆盖无关文件或用户改动。
- R5. 完成技能安装位置、`SKILL.md` 可发现性、`AGENTS.md` 规则内容和 Git diff 范围验证。

## Constraints

- 本次授权只包含本地修改。
- 不执行 `git commit`、`git push`，不创建或更新 Pull Request。
- 不创建、移动或删除任何 worktree。
- 本轮只完成 Phase 1 规划；在用户确认规划前，不安装技能、不修改根目录 `AGENTS.md`，也不运行 `task.py start`。

## Acceptance Criteria

- [x] `.agents/skills/codebase-onboarding/SKILL.md` 存在且可从项目技能目录发现。
- [x] `codebase-onboarding` 的安装文件全部位于当前仓库项目范围内，没有写入用户级技能目录。
- [x] 根目录 `AGENTS.md` 包含独立的“完整项目知识地图”组合技能规则，并按 R2 的顺序和职责描述五个技能。
- [x] 新规则明确与现有“技术分析方案文档组合技能”和“前端组合技能”协同，且现有规则内容未被覆盖或重复改写。
- [x] `git diff --check` 通过，Git diff 只包含 Trellis 任务规划材料、`AGENTS.md` 与新安装的 `codebase-onboarding` 技能文件。
- [x] 任务始终保持本地交付边界：没有新增提交、没有推送、没有 PR 变更。

## Out of Scope

- 安装或实现 `web-design-engineer`。
- 实际生成项目知识地图、架构图或知识网站。
- 修改现有技术分析文档或前端实现工作流。
- 提交、推送或创建 Pull Request。
