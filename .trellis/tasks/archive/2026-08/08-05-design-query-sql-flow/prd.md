# 设计自然语言查询到 SQL 的生成校验流程

## Goal

基于用户提供的“关键词抽取 → 元数据召回 → 上下文补充 → SQL 生成/校验/修正/执行”流程，结合当前 Data Agent 仓库的真实能力，形成一套可落地、可验证且边界清晰的自然语言查询到 SQL 方案，并在现有流程存在结构性缺口时给出更优流程建议。

## Background

- 用户提供的参考流程包含：抽取关键词；并行召回字段信息、指标信息和字段取值；合并召回信息；过滤指标信息和表格信息；增加额外上下文；生成 SQL；校验 SQL；有误时校正 SQL；无误时执行 SQL。
- 用户允许创建 Trellis 任务并进入 planning。
- 方案和流程图完成后，用户已于 2026-08-06 明确授权进入实现；提交、推送和 Pull Request 仍需单独授权。

## Confirmed Facts

- 当前 Chat 仅围绕 source/DDL 提供语义协作，系统提示明确禁止编造业务行结果；仓库当前没有 DW row-query 或 SQL executor（`backend/src/chat/service.py:32-36`，`docs/agent-knowledge.html:163-189`）。
- 当前 Answer Readiness 只负责识别数据依赖并检查 Data Sync 状态；只有 `streaming` 才放行，不能把“可回答”解释为“已经查询数据”（`backend/src/answer_readiness/service.py:30-61`）。
- 当前 Meta Projection 已提供表、字段、指标的一次有界混合召回，并在返回前回读 MySQL 权威状态（`backend/src/ddl_metadata/meta_projection/application/search.py:67-85`）。
- 字段值召回必须先限定候选 `column_ids`，并返回 `complete` 表示相关 DW 表和值投影是否完整；因此字段值召回不能与字段识别无条件并行（`backend/src/ddl_metadata/meta_projection/application/search.py:87-138`）。
- Meta 语义对象包含 `table`、`column`、`metric`；指标候选携带事实表和关联字段标识，可用于后续上下文扩展（`backend/src/ddl_metadata/meta_projection/models.py:11-17,119-132`）。
- Meta 语义检索已经使用 Qdrant dense + BM25 的 RRF 融合；方案无需新增关键词检索基础设施（`backend/src/ddl_metadata/meta_projection/qdrant.py:142-203`）。
- DW 采用不带来源前缀的统一目标表，查询执行目标应是 DW，而不是直接访问源业务库（`backend/src/data_sync/models.py:63-101`）。
- `sqlglot` 已作为后端依赖并用于确定性 DDL 解析，可复用于 SELECT AST 校验，不需要新增 SQL 解析依赖（`backend/pyproject.toml:27`）。

## Requirements

- 方案必须先核验仓库中的现有元数据、指标、字段取值、检索、LLM、SQL 解析/校验、数据同步与查询安全能力，不得把尚不存在的能力描述为现状。
- 方案必须明确每个阶段的输入、输出、职责、失败分支和关键约束。
- 用户问题必须先转换为严格 `QueryIntent`，所有关键短语必须来自用户原文；模型不得在该阶段生成数据库对象 ID 或补充未表达的默认条件。
- `QueryIntent` 必须通过真实 Meta 候选绑定为唯一对象；指标、时间、维度或过滤条件仍有歧义时，每轮只提出一个最高影响的澄清问题，完成后重新绑定，不生成 SQL。
- 方案必须评价参考流程中可保留、应合并和需新增的环节，并推荐最小可行流程结构。
- 方案必须覆盖 SQL 生成后的闭环：校验、有限次修复、执行前安全门禁、执行与结果处理；不得形成无限修复循环。
- 通过全部确定性门禁后自动执行只读 SQL，不增加用户确认节点。
- 不得用固定 `LIMIT` 截断业务结果总量。用户明确的 Top-N 必须保留；明细结果由执行层按固定批次分页或流式读取，并保持后续结果可继续获取。
- 方案必须区分确定性程序能力与 LLM 能力，优先用确定性规则完成解析、权限、安全和执行门禁。
- 方案必须输出独立 SVG 流程图，并提供便于评审的 PNG 版本。

## Acceptance Criteria

- [x] 给出现状证据清单，包含关键文件、接口或领域契约定位。
- [x] 给出推荐流程，阶段输入/输出、失败路径和人工介入条件清晰。
- [x] 明确指出参考流程至少一个结构性风险及对应改进。
- [x] 自动执行路径具备 SQL AST 白名单、实际数据库预检、DW 就绪、最小权限连接、资源预算和审计门禁。
- [x] 结构化问题理解、真实对象绑定和逐项歧义澄清均有明确 fail-closed 规则。
- [x] 结果传输预算不改变聚合、明细或 Top-N 的业务语义。
- [x] 明确 MVP 与后续增强项，避免把检索、规划、修复等能力一次性过度建设。
- [x] 生成可打开、无元素重叠的 SVG，并成功转换为 @2x PNG。
- [x] Trellis planning 文档通过校验，且未在用户批准前进入实现阶段。

## Out of Scope

- 不实现前端 Query UI、通用权限系统或新的检索基础设施；查询执行仅依赖专用 DW SELECT-only 数据库账号。
- 本轮不提交、推送或创建 Pull Request，除非用户后续明确授权。
- MVP 默认查询统一 DW 的全来源数据；按单一来源过滤需要独立设计 owner join 或新的来源维度，不在本轮实现范围内。

## Product Decisions

- 用户明确选择：通过门禁后自动执行只读 SQL，不要求用户确认。
- 用户明确授权实现本方案，但未授权提交、推送或创建 Pull Request。
- 保持当前 `chat-turns` 契约不变；未来实现使用独立 Query module/interface 和 `query-turns` 路由，现有 Chat UI 接入不属于本轮范围。
