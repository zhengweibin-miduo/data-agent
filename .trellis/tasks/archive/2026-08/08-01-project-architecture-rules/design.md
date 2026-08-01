# 项目架构与职责边界规则设计

## 目标规则位置

在根目录 `AGENTS.md` 的“前端组合技能”之后、“完整项目知识地图组合技能”之前新增独立的“项目架构与职责边界组合技能”段落。该段落只在任务明确涉及项目架构、源码根目录、模块职责、依赖方向、领域模型或跨端契约时触发，不吞并其他组合技能。

## 组合技能顺序

四个 Skill 按需触发，不是每次架构任务都全部执行；多个环节同时适用时遵循以下顺序，并完整遵守各 Skill 自身规定的产物与停止点：

1. `architecture-blueprint-generator`：仅在用户要求完整架构蓝图或同等范围参考文档时使用，并生成其规定的完整蓝图与图示；普通局部取证直接读取真实仓库证据，不借用该 Skill 名义。
2. `domain-modeling`：所有相关任务先读取 `CONTEXT.md`；仅在需要澄清、改变或新增领域语言、模型或上下文关系时使用，并即时更新 `CONTEXT.md`。只读取既有词汇不构成使用该 Skill。
3. `architecture-patterns`：在设计或实质重构后端服务/模块、实施 DDD/Ports and Adapters 或排查依赖环时使用，明确层次、端口/适配器、向内依赖和测试边界。
4. `improve-codebase-architecture`：因 Skill 禁止自动触发，只在用户明确要求执行该 Skill 或其定义的架构改进扫描时使用；一旦调用，必须读取 `CONTEXT.md` 与相关 ADR，使用 `codebase-design` 的规定词汇，生成仓库外的临时 HTML 候选报告、等待用户选择，再进入 grilling，不得自动重构。用户未请求该扫描时不调用，因此普通架构任务不强制 HTML 报告。

## 源码根目录与跨端 seam

```text
frontend/              React/Vite/TypeScript 前端源码、静态资源、构建/部署配置与前端测试
src/data_agent/         Python 后端源码、运行入口与后端业务能力
contracts/              可选；仅存技术中立的契约源或生成配置，不存业务源码
```

- 前端不得导入 `src/data_agent/` 中的 Python 源码、ORM 模型或后端内部 DTO。
- 后端不得依赖 `frontend/` 的组件、状态模型或构建产物来表达业务行为。
- 跨端只通过 HTTP/SSE 等运行时协议以及 OpenAPI、JSON Schema 或生成客户端等显式契约交互。
- 契约的权威来源和生成方向必须唯一；生成文件不得反向成为领域模型的权威来源。

## 后端 DDD 与 Ports and Adapters

后端保持模块化单体；bounded context 优先于横向技术目录。新建或实质重构的业务模块应形成以下职责：

- `domain`：实体、值对象、聚合、领域规则和领域事件；不依赖 FastAPI、SQLAlchemy、Redis、外部 SDK 或配置加载。
- `application`：用例编排、事务边界和端口定义；依赖领域模型和抽象端口，不依赖具体适配器。
- `adapters`：实现输入/输出端口，负责 HTTP/SSE、持久化、消息、搜索和外部服务之间的转换；不得承载领域规则。
- `infrastructure`：数据库连接、客户端和框架配置等低层驱动与资源；不得定义用例或领域规则。
- 组合根：只在应用启动位置选择具体适配器并完成依赖注入。

内层不得导入外层；领域层与应用层不得依赖适配器或基础设施，外层通过应用端口进入内层。不同 bounded context 默认不直接共享实体或 ORM 模型，通过标识符、领域事件或 ACL 交互；确需 Shared Kernel 时必须范围极小、双方共同维护并记录决策。现有代码允许渐进迁移，规则不得宣称仓库已经全面满足严格 DDD，也不得要求一次性空目录重排。

## 前端 feature-first 架构

当前规模采用轻量 feature-first，而不是完整 Feature-Sliced Design：

- 应用壳负责启动、路由、全局 provider 和跨 feature 导航。
- feature 目录按用户能力组织页面编排、feature 状态和专属展示模块。
- 共享 API 模块负责 HTTP/SSE、契约类型、错误映射与请求生命周期，不包含页面状态和业务编排。
- 共享 UI 只保存真正被多个 feature 复用的无业务展示模块；不得提前抽象。
- URL、React 状态、ref、sessionStorage/localStorage 和后端权威状态必须各有唯一所有者。
- feature 之间通过公共 interface 协作，不导入对方内部实现。

## 与现有规则的关系

- 需要技术分析方案文档时，仍使用 `create-implementation-plan`，本组合不替代它。
- 涉及页面/组件设计或实现时，仍执行 `frontend-design → 实现 → web-design-guidelines`。
- 需要完整项目知识地图时，仍执行既有知识地图组合；本组合不自动生成网站、SVG 或 onboarding artifacts。
- Trellis 的规划、激活、实现、检查和收尾阶段保持不变。

## 兼容性与回退

本次只新增代理规则，不改变运行时、包路径或构建配置。回退方式是删除新增的独立段落；不会产生数据、接口或部署迁移。
