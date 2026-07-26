# 为核心业务流程补充中文注释

## Goal

为项目后端核心业务流程补充有解释价值的中文注释，使维护者既能沿真实调用链理解跨模块阶段，也能在复杂函数内部按步骤理解数据筛选、校验、转换和持久化过程。

## Background

- 用户确认完整覆盖已识别的六类后端核心流程。
- 仓库是纯 Python 后端项目，不存在前端应用；本任务不创建或覆盖前端代码（`.trellis/spec/backend/directory-structure.md:5-11`）。
- 项目规范要求中文 Google Style Docstring；行内注释只解释设计理由与不变量，不复述可见代码行为（`.trellis/spec/backend/quality-guidelines.md:63-74`）。
- 长期记忆加载、混合检索和索引 outbox 已有较完整的阶段说明，重复补注释会降低信息密度。

## Requirements

### R1：DDL 任务受理边界

在 `src/data_agent/ddl_metadata/api/jobs.py` 和 `jobs/store.py` 说明 HTTP `202`、Redis 原子状态/source lease/dispatch outbox 与异步 worker 之间的受理边界，以及 SSE 公开投影与内部 graph stream 的区别。

### R2：Worker 与 LangGraph 执行恢复

在 `src/data_agent/ddl_metadata/worker/job_runner.py` 说明 revision/租约守卫、首次执行与 checkpoint 恢复、interrupt/resume、公开阶段投影、瞬态重试与终态清理之间的关键不变量。

### R3：LangGraph 拓扑

在 `src/data_agent/ddl_metadata/workflow/graph.py` 说明模型输出受确定性校验约束、缺失业务含义时等待人工回答，以及验证通过后才进入唯一持久化出口。

### R4：Conversation 上下文

在 `src/data_agent/conversation/service.py` 说明用户消息事务与上下文构建边界，以及摘要、游标后近期消息、同用户长期记忆三类来源如何受数量和字符预算约束；在 `src/data_agent/conversation/extraction.py` 等复杂 Conversation 函数内部，按阶段说明候选规范化、消息归属与角色校验、精确 quote、助手结论后续确认、逻辑作用域去重和长期记忆构建。

### R5：应用资源生命周期

在 `src/data_agent/application.py` 说明外部资源初始化、`app.state` 服务装配与逆序关闭的依赖关系。

### R6：Meta 快照持久化

在 `src/data_agent/ddl_metadata/persistence/snapshots.py` 和 `metadata_repository.py` 说明同一 MySQL 事务内的作用域指纹过期、Meta 同步、权威记忆与双索引 outbox 顺序，以及关联清理为何严格限制在本次提交范围内。

### R7：复杂函数内部步骤

- 对核心流程中包含多个连续筛选、校验、状态转换或持久化阶段的复杂函数，在关键代码块前增加中文步骤注释。
- 步骤注释应让读者能从上到下还原流程，但不要求每行或每个简单条件都添加注释。
- 不使用表示“尚未完成”的 `TODO` 标记；使用“步骤一/二”或直接描述阶段目的的注释。
- 业务 CRUD 方法同样使用“步骤一、步骤二……”说明读取、校验、写入、回读或删除顺序。
- 生产代码中未编号、独立存在的说明性行内注释必须删除，或合并到对应步骤注释；`# noqa`、类型忽略和覆盖率等工具指令除外。

### R8：注释质量与行为不变

- 新增说明使用中文；框架、协议、符号名和固定技术术语可保留英文。
- 扫描全部项目自有 `src/data_agent/**/*.py`，将英文注释和 Docstring prose 翻译为中文；`Args:`、`Returns:`、`Yields:`、`Raises:` 章节名及固定技术术语保留英文。
- 第三方依赖、虚拟环境、生成代码和外部库悬浮提示不在修改范围内。
- 注释解释“为什么”、阶段边界和不变量，不复述显而易见代码。
- 将复杂函数的分步骤注释约定同步到后端质量规范。
- 不重构、不格式化、不修改测试、配置或业务逻辑。

## Acceptance Criteria

- [x] AC1：R1-R6 的核心流程均有与真实调用链一致的跨模块说明。
- [x] AC2：核心复杂函数和业务 CRUD 在关键阶段具有可顺序阅读的中文编号步骤注释。
- [x] AC3：除工具指令外，生产代码不存在未编号、独立存在的说明性行内注释或英文注释/Docstring prose。
- [x] AC4：新增注释符合中文 Docstring 和统一编号步骤注释契约，且不逐字复述表达式。
- [x] AC5：相对 `origin/master`，所有变更产品 Python 文件在剥离 Docstring 后的 AST 完全一致。
- [x] AC6：Ruff、Pyright、compileall、非集成 pytest 和 `git diff --check` 通过。

## Out of Scope

- 业务逻辑重构、功能变更或错误修复。
- 前端、测试、配置、SQL、一般文档和与核心流程无关的模块；本任务产生的新注释约定同步到 Trellis 质量规范。
- `.venv/`、全局 `site-packages` 和其他第三方依赖中的英文官方 Docstring。
- 为单行表达式逐字复述代码；CRUD 仍需按业务阶段提供编号步骤注释。
