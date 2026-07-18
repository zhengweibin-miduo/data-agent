# 基于 Mem0 重构项目记忆架构

## Goal

将 Data Agent 当前错误参考 `usememos/memos` 的长期记忆设计，重新规划并实现为参考用户指定的 [`mem0ai/mem0`](https://github.com/mem0ai/mem0) 的 AI Agent 记忆架构。改造后的记忆能力必须服务于 MySQL DDL 语义元数据与指标生成场景，并保持工作流恢复、确定性校验和 Meta 快照一致性。

## Background

- 用户明确指定参考对象是 `mem0ai/mem0`，不是 `usememos/memos`。
- 当前实现的 `llm_memory`、`llm_memory_relation`、规范 `content`、可重建 `payload`、pin/archive 和类型化关系，来源于对 `usememos/memos` 笔记数据模型的适配。
- 当前短期任务状态由 LangGraph Redis checkpoint 保存；长期记忆保存在 MySQL，并通过来源、作用域和 Schema fingerprint 精确检索。
- Qdrant、Elasticsearch 和 TEI 已有配置及基础设施适配器，但尚未进入记忆主链路。
- 现有 Qdrant、Elasticsearch 和 TEI 代码只管理客户端生命周期；尚无索引结构、写入、检索、融合、outbox 或重建实现。
- Mem0 是面向 AI Agent 的记忆层，包含记忆抽取、作用域管理、添加与检索以及向量/关键词/实体等检索能力；不能通过重命名现有模型完成适配。
- 首次源码研究固定到 `mem0ai/mem0` 提交 `ddaa655edf41e3ed375b263fb227da0bcd42ccb9d`，防止上游主分支变化静默改变本任务设计。

## Requirements

### R1. 参考来源

- 设计和实现以 `mem0ai/mem0` 的开源代码及官方文档为参考。
- 参考 Mem0 的开源架构并在现有 Data Agent 服务边界内领域化实现，不直接引入或运行 `mem0ai` SDK。
- 删除设计资料中把 `usememos/memos` 表述为目标记忆架构来源的错误结论。
- 对直接借鉴、领域化改造和项目自有机制进行明确区分。

### R2. 领域边界

- 记忆内容只包含通过当前 Pydantic 契约、DDL AST 和确定性规则验证的语义事实、指标定义与显式用户回答。
- 不保存隐藏思维链、未验证模型输出、完整 Prompt、无界对话记录或敏感配置。
- 采用三层记忆结构：
  - 工作记忆：当前节点、重试、校验错误和 interrupt 状态，由 Redis LangGraph checkpoint 保存；
  - 会话/情景记忆：当前 DDL、问题、回答和候选事实，按 `job_id` 保存在活动任务 checkpoint；
  - 长期语义记忆：跨任务复用的表列语义、业务回答和指标定义，以 MySQL 为权威存储，并建立 Elasticsearch BM25 与 Qdrant 向量投影。
- LangGraph checkpoint 继续负责活动任务的工作状态与恢复，不与跨任务长期记忆混为一谈。
- `job_id` 是短期任务作用域；逻辑 DDL `source` 是长期记忆的主要作用域。
- 任务成功并通过确定性校验前，候选事实不得进入长期权威存储或可召回向量索引。

### R3. Mem0 能力映射

- 规划 Mem0 的记忆抽取、作用域、写入、搜索、更新/删除或历史机制如何映射到 DDL 元数据场景。
- 复用现有 LangGraph 结构化生成与确定性校验作为记忆抽取边界，不为最终结果增加第二次通用 LLM 记忆抽取。
- 仅从最终接受的类型化内容确定性生成有界 `memory_text`、检索 metadata 和 embedding；原始类型化内容仍是权威事实。
- 采用 Mem0 的 ADD-only、内容去重、历史记录、关联和语义检索思想，但不允许文本投影反向覆盖类型化事实。
- 将逻辑 DDL `source` 作为跨任务记忆的主要身份作用域；`job_id` 只作为运行来源和 checkpoint 身份，不作为长期召回边界。
- 明确用户、会话、Agent 等 Mem0 作用域与本项目 `source`、对象、任务之间的对应关系。
- 首个版本接入 Mem0 风格的作用域过滤和语义检索：TEI 生成向量，Qdrant 保存可重建投影。
- 首个版本同时接入 Elasticsearch BM25 检索，并将 Elasticsearch、Qdrant 与确定性对象关联作为多信号召回来源。
- 多信号召回必须定义稳定、可测试的分数归一化与融合规则；任一派生索引不可用时，不得把未经 MySQL 权威内容和当前结构校验的结果注入模型。
- 召回必须先限制 `source`、类型、状态和版本，再按相似度选择候选，最终通过当前 DDL AST、Pydantic 契约和确定性规则重校验。
- MySQL 是长期记忆唯一权威来源；Elasticsearch/Qdrant 故障、延迟或重建不能改变已接受语义事实。
- 表、列和指标稳定 ID 作为首版确定性实体关联；不增加通用自然语言实体抽取。
- 首版仅保留创建、更新、生成任务和版本等时间/来源信息，不实现 Mem0 托管平台的时间推理或时间衰减排序。

### R4. 兼容与迁移

- 当前长期记忆尚未投入使用，不保留现有 `usememos/memos` 风格记忆数据。
- 允许删除并重建现有长期记忆表、关系和派生索引，直接建立新的 Mem0 风格模型，不实现旧数据迁移或双读兼容层。
- 本地初始化脚本、SQLAlchemy 表定义、API、测试和架构文档必须同步切换，避免残留两套契约。
- 重建过程必须显式清理或重新创建 Elasticsearch 索引与 Qdrant collection，不能让旧投影进入新召回结果。
- 保持已验证 Meta 快照与相应长期记忆的一致性，不允许部分写入形成不可信上下文。
- 保持重试、恢复和重复执行的幂等安全。
- Meta、MySQL 权威记忆和索引 outbox 在一个 MySQL 事务中提交。
- Elasticsearch/Qdrant 采用 outbox 驱动的最终一致性同步；索引或 TEI 暂时不可用不把已完成的 DDL 主任务改成失败。
- worker 必须重试索引积压并支持全量重建；只有确认完成的索引投影可以参与召回。
- 混合检索结果必须回查 MySQL 权威内容并重校验；派生索引不可用时回退到 MySQL 精确检索。

### R5. 实现范围

- 更新记忆模型、服务、持久化、工作流接入、配置、数据库脚本和测试中受到新设计影响的部分。
- 同步更新架构手册及任务设计资料中的记忆来源和真实运行机制。
- 对外提供领域安全的 Mem0 风格记忆能力：
  - `search`：按查询、`source`、类型等条件执行 Elasticsearch + Qdrant 混合检索；
  - `get`：读取 MySQL 权威结构化记忆；
  - `history`：读取 ADD、修正和删除历史；
  - `update`：提交用户确认的结构化修正，并要求重新处理 DDL 后才进入 Meta；
  - `delete`：执行可审计的软删除并从未来召回中排除。
- 不开放任意长期记忆 `add` API；新增长期事实只能由通过完整校验的 LangGraph 工作流创建。
- API 不返回原始 Prompt、隐藏推理、无界任务状态或敏感配置。

## Acceptance Criteria

- [ ] 仓库设计资料不再把 `usememos/memos` 当作目标记忆架构来源。
- [ ] 新建环境只创建 Mem0 风格的长期记忆、历史/outbox 表和对应 ES/Qdrant 索引，不创建旧记忆契约。
- [ ] 重建流程能够清除旧派生索引，不要求迁移尚未投入使用的旧长期记忆数据。
- [ ] 新设计能够逐项说明与 `mem0ai/mem0` 的对应关系及项目改造边界。
- [ ] API 支持受约束的 search/get/history/update/delete，且不存在绕过 DDL 校验写入任意长期事实的 add 接口。
- [ ] 活动任务 checkpoint 与跨任务长期记忆仍具有独立、清晰的生命周期。
- [ ] TEI/Qdrant 语义召回按长期 `source` 作用域过滤，并且召回结果必须经过当前结构重校验。
- [ ] Elasticsearch BM25 与 Qdrant 向量结果通过稳定、可测试的融合规则合并。
- [ ] Elasticsearch/Qdrant 投影可以从 MySQL 权威记忆重建，索引失败不会产生部分可信事实。
- [ ] MySQL 提交成功而 ES/Qdrant/TEI 暂时失败时，DDL 任务仍成功且索引 outbox 保留可重试积压。
- [ ] 派生检索服务不可用时，系统安全回退到 MySQL 精确检索。
- [ ] 通过重复 DDL、结构变化、用户修正、检索冲突、重试和持久化失败测试。
- [ ] 未验证或失败输出不能进入可复用长期记忆。
- [ ] Meta 和记忆不会出现部分提交。
- [ ] Ruff、Pyright、相关单元测试与可运行的集成测试通过。

## Out of Scope

- 复制 Mem0 托管平台的闭源能力。
- 将本项目改造成通用聊天机器人或任意用户偏好记忆服务。
- 保存隐藏思维链或未通过业务校验的模型推断。
- Mem0 托管平台的时间推理、自动衰减和闭源排序能力。
