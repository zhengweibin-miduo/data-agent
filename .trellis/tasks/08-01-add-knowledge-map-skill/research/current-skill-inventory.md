# 当前技能组合核验

核验日期：2026-08-01

## 仓库基线事实

- 根目录 `AGENTS.md` 已包含“技术分析方案文档组合技能”和“前端组合技能”两组规则。
- `.agents/skills/` 已存在 `domain-modeling`、`codebase-design`、`baoyu-diagram`、`frontend-design` 和 `web-design-guidelines`。
- `.agents/skills/` 当前未发现 `codebase-onboarding` 或 `web-design-engineer`。

## 上游能力核验

- ECC 上游 `skills/codebase-onboarding/SKILL.md` 将该技能定位为陌生代码库勘察与 onboarding 指南生成，覆盖技术栈、架构模式、关键目录、入口、约定，以及一次请求从入口到响应的数据流追踪。
- 上游来源：https://github.com/affaan-m/ECC/blob/main/skills/codebase-onboarding/SKILL.md

## 对实施的约束

- 安装后应以本地实际 `SKILL.md` 为最终事实来源，不能仅依赖上游快照。
- `AGENTS.md` 新规则应把 `codebase-onboarding` 限定为真实仓库证据采集与结构/流向梳理，避免与 `domain-modeling`、`codebase-design` 和现有文档/前端规则职责重叠。
- `web-design-engineer` 的缺失只作为当前基线差异记录；本任务不获得安装该技能的授权。
