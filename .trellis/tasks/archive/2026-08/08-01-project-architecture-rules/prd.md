# 制定项目架构与职责边界规则

## Goal

在根目录 `AGENTS.md` 中建立可执行的项目架构与职责边界规则：前后端源码物理分离，后端以 DDD 与 Ports and Adapters 为目标架构，前端采用与当前 React/Vite 项目规模匹配的 feature-first 结构，并使用项目领域语言约束业务模型。

## Confirmed Facts

- 本任务只修改项目级代理规则和 Trellis 规划记录，不执行前后端源码迁移或 DDD 重构。
- 前后端根目录采用现有 `frontend/` 与 `src/data_agent/`；源码隔离以所有权和禁止跨根源码导入为准，不为目录名称对称而迁移 Python 包。
- 当前后端源码根目录是 `src/data_agent/`，整体为模块化单体，部分模块已有 domain/application/port 痕迹，但尚未全面遵守严格 DDD 依赖反转。
- PR #76 正在把 React/Vite/TypeScript 前端迁移到独立 `frontend/` 根目录；其现有结构是按用户工作区组织的 feature-first 雏形，并通过 `frontend/src/api/` 访问后端。
- `CONTEXT.md` 已定义 DDL Metadata、Conversation、Long-term Memory、Meta Snapshot 和 Memory Projection 等统一领域术语。
- 架构规则按各自真实触发条件组合 `architecture-blueprint-generator`、`domain-modeling`、`architecture-patterns` 与 `improve-codebase-architecture`；它们不是每次全部执行，也不得替代现有 Trellis、前端设计审查、技术方案文档或完整知识地图工作流。

## Requirements

- **R1 — 源码根目录隔离**：规则必须禁止前端业务源码与后端业务源码混放、互相直接导入；跨端交互只能通过显式契约和运行时协议。
- **R2 — 后端 DDD 边界**：规则必须定义领域层、应用层、适配器层、基础设施/组合根的职责与向内依赖原则，并要求按 bounded context 组织业务能力。
- **R3 — 领域模型纪律**：所有相关任务必须先读取 `CONTEXT.md`；仅在主动澄清或改变领域模型时使用 `domain-modeling`，并按其规则即时维护统一语言与必要决策。不得把 DTO、ORM 模型或外部响应当作领域模型。
- **R4 — 前端架构**：规则必须采用适合当前规模的 feature-first 组织，明确页面/功能编排、共享 API 适配、状态所有权和公共接口边界；不得无条件引入完整 Feature-Sliced Design 层级。
- **R5 — 跨端契约**：规则必须声明前端不得导入后端 Python 源码，后端不得依赖前端实现；OpenAPI、JSON Schema 或生成客户端等契约应保持技术中立并拥有明确的生成/所有权规则。
- **R6 — 组合技能顺序**：规则必须说明四个目标 Skill 各自的真实触发条件、多个环节同时适用时的执行顺序、职责和停止点，避免无条件触发或重复现有组合技能。
- **R7 — 架构评估约束**：`improve-codebase-architecture` 禁止自动触发；用户明确调用后必须按 Skill 生成临时 HTML 候选报告并等待选择，不得被描述为无需其规定交付物的局部扫描器或自动重构器。

## Acceptance Criteria

- [x] `AGENTS.md` 明确区分前端、后端和可选契约根目录，禁止跨根目录源码耦合。
- [x] `AGENTS.md` 明确后端 DDD/Ports and Adapters 的层职责和允许的依赖方向。
- [x] `AGENTS.md` 明确前端 feature-first 的目录职责、API 边界和状态所有权原则。
- [x] `AGENTS.md` 明确读取 `CONTEXT.md` 与主动使用 `domain-modeling` 的区别，以及后者在业务模型和统一语言中的职责。
- [x] 四个 Skill 的按需触发、顺序和职责与真实 `SKILL.md` 一致，且与现有技术方案、前端设计审查、完整知识地图组合规则无冲突。
- [x] 规则不宣称当前代码已经完全满足 DDD，也不把本任务扩大为源码迁移。
- [x] Markdown 结构清晰，相关路径、Skill 名称和现有项目事实经仓库证据核验。

## Out of Scope

- 移动 `src/data_agent/` 或 `frontend/` 中的源码。
- 将现有所有后端模块一次性重构为严格 DDD。
- 新建前端页面、视觉设计或组件实现。
- 生成完整项目知识地图、架构网站或持久化 SVG 图。
