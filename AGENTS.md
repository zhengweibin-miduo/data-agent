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

## 项目架构与职责边界组合技能

仅当任务明确涉及项目架构、源码根目录、模块职责、依赖方向、领域模型或跨端契约时应用本节。开始架构判断前必须先核验真实仓库结构、依赖、请求流和数据流；下列 Skill 按各自触发条件使用，并非每次全部执行。多个环节同时适用时按以下顺序，且不得以局部调用的名义跳过 Skill 自身规定的交付物或停止点：

1. **完整蓝图**：仅当任务要求产出完整项目架构蓝图或同等范围的架构参考文档时，先使用 `codebase-onboarding` 的 Phase 1（Reconnaissance）与 Phase 2（Architecture Mapping）核验技术栈、结构、依赖、请求流和数据流，再使用 `codebase-design` 明确模块边界，并按需使用 `baoyu-diagram` 生成图示；不得执行 `codebase-onboarding` 的后续阶段或生成 onboarding artifacts。普通的局部架构取证直接基于仓库证据完成，不触发该组合。
2. **领域建模**：所有相关任务先读取根目录 `CONTEXT.md` 作为统一语言；仅当任务需要澄清、改变或新增领域术语、模型或上下文关系时，使用 `domain-modeling`，在结论形成时按该 Skill 立即更新 `CONTEXT.md`，并只在满足其条件时记录 ADR。只消费既有术语不算使用该 Skill。DTO、ORM 模型、外部服务响应和生成的契约类型不得代替领域模型。
3. **后端模式设计**：仅当设计新后端服务或模块、按 bounded context 实质重构单体、实施 DDD/Ports and Adapters，或排查跨层依赖环时，使用 `codebase-design` 评估 module、interface、seam、adapter、leverage 与 locality；产出清晰的层次、端口/适配器接口、依赖规则和测试边界，不宣称现有代码已经全面满足严格 DDD。
4. **深模块评估**：`improve-codebase-architecture` 禁止自动触发；仅当用户明确要求执行该 Skill 或进行其定义的架构改进扫描时使用。调用后必须先读取 `CONTEXT.md` 与相关 ADR，并使用 `codebase-design` 的 module、interface、depth、seam、adapter、leverage、locality 词汇；随后按 Skill 生成仓库外的临时 HTML 候选报告并等待用户选择，再进入 grilling。它不是自动重构器。未请求该扫描时，不强制生成 HTML 报告，可在当前任务范围内直接给出有证据的架构判断。

源码根目录按所有权隔离：

- 当前框架无关前端迁移完成前，`src/data_agent/frontend/` 仍是前端源码与运行时静态资源的唯一所有者，并继续由 FastAPI 挂载和随 Python 包分发；在此期间前端修改必须遵守 `.trellis/spec/frontend/`，不得提前写入尚未接入运行时的根目录 `frontend/`。
- 只有 React/Vite/TypeScript 前端迁移、FastAPI 静态资源挂载切换、构建部署与测试路径调整在同一变更中完成并验证后，根目录 `frontend/` 才成为前端应用源码、静态资源、构建/部署配置和前端测试的唯一长期所有者。
- 除迁移完成前由上一条明确保留的 `src/data_agent/frontend/` 外，`src/data_agent/` 是 Python 后端源码、运行入口和后端业务能力的唯一长期所有者；迁移完成后不得在其中新增或恢复前端业务源码。
- `contracts/` 为可选目录，仅在需要时承载技术中立的 OpenAPI、JSON Schema 等契约源或生成配置，不得承载前端或后端业务源码。
- 前端不得直接导入 `src/data_agent/` 中的 Python 源码、ORM 模型或内部 DTO；后端不得依赖 `frontend/` 的组件、状态模型或构建产物表达业务行为。跨端交互只能通过 HTTP、SSE 等运行时协议及显式契约完成。
- 每项跨端契约必须有唯一的权威来源、明确的所有者和单向生成规则；生成客户端或生成类型不得反向成为领域模型的权威来源。

后端保持模块化单体并按 bounded context 组织业务能力，采用渐进式 DDD 与 Ports and Adapters：新建或实质重构的业务模块中，`domain` 负责实体、值对象、聚合、领域规则和领域事件，不依赖 FastAPI、SQLAlchemy、Redis、外部 SDK 或配置加载；`application` 负责用例编排、事务边界以及 driving/driven ports 的定义，只依赖领域模型和抽象端口；`adapters` 实现输入或输出端口，负责 HTTP/SSE、持久化、消息、搜索和外部服务之间的转换，不承载领域规则；`infrastructure` 只提供数据库连接、客户端、框架配置等低层驱动与资源，不定义用例或领域规则；组合根只在应用启动位置选择具体适配器和基础设施资源并完成依赖注入。内层不得导入外层，领域层与应用层不得依赖 `adapters` 或 `infrastructure`；外层通过应用端口进入内层。不同 bounded context 默认不直接共享实体或 ORM 模型，通过标识符、领域事件或防腐层协作；确需 Shared Kernel 时必须范围极小、双方共同维护并记录决策。现有模块按实际改动渐进迁移，不要求一次性重排目录或创建空层。

前端采用与当前规模匹配的轻量 feature-first，而不无条件引入完整 Feature-Sliced Design 层级：应用壳负责启动、路由、全局 provider 和跨 feature 导航；feature 按用户能力组织页面编排、feature 状态和专属展示模块；共享 API 层负责 HTTP/SSE、契约类型、错误映射和请求生命周期，不包含页面状态或业务编排；共享 UI 只保存确实被多个 feature 复用的无业务展示模块，不提前抽象。URL、React 状态、ref、`sessionStorage`/`localStorage` 与后端权威状态必须各有唯一所有者；feature 之间只通过公共接口协作，不导入彼此内部实现。

本组合与既有规则协同：需要技术分析方案文档时仍使用 `create-implementation-plan`；涉及页面或组件设计、实现、审查时仍执行 `frontend-design → 实现 → web-design-guidelines`；需要完整项目知识地图时仍执行下方知识地图组合，本组合不自动生成网站、SVG 或 onboarding artifacts；Trellis 的规划、激活、实现、检查和收尾阶段保持不变。

## 完整项目知识地图组合技能

仅当任务需要产出“完整项目知识地图”时，按以下顺序组合使用项目级 Skill；该组合与既有“技术分析方案文档组合技能”和“前端组合技能”协同，不替换或重复其规则：

1. **代码库勘察**：先使用 `codebase-onboarding` Skill（`.agents/skills/codebase-onboarding/SKILL.md`）的 Phase 1（Reconnaissance）与 Phase 2（Architecture Mapping），仅基于真实仓库证据梳理结构、入口、模块职责、请求流与数据流；不得执行后续阶段、生成 onboarding artifacts 或创建、修改 `CLAUDE.md`。
2. **领域统一**：先读取 `CONTEXT.md` 使用既有统一语言；发现术语模糊、冲突或确需新增领域概念与关系时，再使用 `domain-modeling` Skill，并按其规则即时更新 `CONTEXT.md`。
3. **边界梳理**：使用 `codebase-design` Skill，明确模块职责、依赖方向与边界。
4. **图示生成**：使用 `baoyu-diagram` Skill，生成专业 SVG 架构图、流程图或时序图。
5. **知识网站呈现**：使用 `web-design-engineer` Skill，实现知识地图的网站视觉呈现（该技能是否已安装由项目现状决定，本规则不扩大安装范围）。
