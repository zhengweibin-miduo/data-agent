# 自然语言查询到 SQL 的推荐流程设计

## 1. 结论

推荐把参考流程调整为两个深模块阶段：

1. **Query Context**：先把完整问题结构化为 `QueryIntent`，逐项消除指标、时间、维度与过滤歧义，再召回并绑定 Meta 候选，确定性限定当前 DDL/source 的对象范围，补齐关系与字段值候选。
2. **Safe Query**：模型只生成结构化 Query Draft；程序完成 AST、依赖、数据库预检、数据就绪和资源门禁，通过后自动执行。

不新增关键词抽取器，不新增搜索引擎，不把现有 Chat 的自由文本生成直接改成 SQL 工具调用。复用现有 `parse_ddl`、Meta Projection、Answer Readiness、Conversation 上下文、`LLMClient` 和 `sqlglot`。

## 2. 参考流程评价

| 参考节点 | 结论 | 推荐调整 |
|---|---|---|
| 抽取关键词 | 删除 | 直接使用完整问题做现有 Dense + BM25 RRF 召回，避免丢失时间、否定、比较和粒度语义。 |
| 字段/指标并行召回 | 合并 | 表、字段、指标已在同一 Meta 语义索引中；一次召回、不同 kind 共用候选预算。 |
| 字段取值并行召回 | 后移 | 现有接口强制要求先有 `column_ids`；字段范围确定后再召回值。 |
| 合并召回信息 | 保留并具体化 | 先按当前 DDL 的 object/table ID allowlist 过滤，再按表归并并扩展 FK 关系。 |
| 过滤指标/表格 | 前移 | source/schema 作用域必须在上下文构建时确定，不能等模型生成 SQL 后过滤。 |
| 增加额外上下文 | 收窄 | 只加入物理类型、主外键、指标定义、候选值、会话业务规则和查询预算。 |
| 校验 SQL | 拆成两层 | 先做 SQLGlot AST 静态校验，再用只读连接执行 `EXPLAIN`。 |
| 校正 SQL | 改成有界闭环 | 最多修复一次；再次失败直接结束，不无限循环。 |
| 执行 SQL | 增加四道门禁 | 实际依赖就绪、只读账号、时间/行数/字节预算、结构化审计。无需用户确认。 |

参考图最大的结构性缺口是“校正 SQL”后没有明确重新校验，而且没有重试上限。第二个缺口是没有区分“SQL 语法正确”和“允许安全执行”。

## 3. 推荐主流程

```text
QueryRequest(question + user/conversation + current DDL/source)
  -> 确定性解析当前 DDL
  -> 判断是否为业务数据查询
       否：沿用当前 DDL Chat
       是：继续
  -> LLM 严格结构化 QueryIntent（仅提取用户原文，不生成对象 ID）
  -> 单次 Meta 混合召回(table/column/metric)
  -> 按当前 DDL object/table allowlist 过滤并归并候选
  -> 每个关键槽位是否唯一绑定？
       否：返回一个最高影响的澄清问题；下一轮合并会话回答后重新绑定
       是：继续
  -> 对候选 column_ids 召回字段值
  -> 用当前 DDL 补齐物理类型、主键、FK 路径
  -> 形成有预算的 QueryContext
  -> LLM 生成严格 QueryDraft(sql + params + referenced IDs)
  -> SQLGlot AST + allowlist 静态校验
  -> 只读连接 EXPLAIN
  -> 校验失败？
       首次：把稳定错误码反馈给模型，修复一次并重新校验
       再次：安全失败
  -> 按已解析 target tables 做 Answer Readiness
  -> 专用 DW 只读连接自动执行
  -> 限制行数/字节，生成 QueryResult
  -> Conversation 完成轮次 + 结构化审计日志
```

## 4. Module、interface 与 seam

新增一个 `query` bounded context。它对调用方只暴露一个深 interface：

```python
class QueryApplication:
    def stream(self, request: QueryRequest) -> AsyncIterator[QueryEvent]: ...
```

调用方不感知检索顺序、修复次数、SQL AST、`EXPLAIN`、readiness 或数据库预算。测试也只通过该 interface 观察结果。

建议文件结构只创建有真实职责的模块：

```text
backend/src/query/
├── domain.py                    # QueryIntent、QueryDraft、ValidatedQuery 与纯校验策略
├── application/
│   ├── contracts.py             # planner、metadata、readiness、executor ports
│   └── service.py               # 唯一查询编排 use case
└── adapters/
    ├── llm.py                   # 严格 structured output 与一次修复
    ├── mysql.py                 # DW 只读 EXPLAIN/execute
    └── http.py                  # 独立查询入口，若产品决定接入 HTTP
```

组合根在 `backend/src/application.py` 选择现有 Meta Search、Readiness、LLM 与 MySQL adapters。不要在 Query module 中构造全局客户端。

### 4.1 可复用能力

| 需要 | 复用位置 | 约束 |
|---|---|---|
| 当前结构与 FK | `ddl_metadata.parsing.parse_ddl` | 保持 DDL-only parser；查询 SQL 使用新的校验函数，不能放宽 `parse_ddl`。 |
| Meta 召回 | `MetadataSearchService.search_metadata` | 一次召回 table/column/metric；结果必须再按当前 DDL object IDs 过滤。 |
| 字段值候选 | `MetadataSearchService.search_values` | 只在字段确定后调用；`complete=false` 时值仅作提示，不能当完整枚举。 |
| 数据就绪 | `AnswerReadinessService` | 依据最终解析的 target tables 调用；不能替代权限或 SQL 安全。 |
| 会话上下文 | `ConversationService` | 复用 turn 幂等、summary、近期消息和用户长期规则。 |
| 模型 | `LLMClient` | 复用零温度、并发和超时配置；只接收有界 QueryContext。 |
| SQL AST | 已安装 `sqlglot` | 新建 SELECT policy；不增加解析依赖。 |

### 4.2 暂不新增的能力

- 不创建独立关键词抽取器。
- 不创建新的向量库、全文索引或 reranker。
- 不持久化新的关系投影：MVP 复用请求携带的当前 DDL FK。
- 不提供单来源行过滤：当前统一 DW 不保存 source 列；MVP 明确查询全来源数据。
- 不新增 durable audit 表：先使用脱敏结构化日志；出现合规留存要求时再增加。

## 5. 核心契约

```python
class QueryRequest(ContractModel):
    user_id: str
    conversation_uid: str
    turn_uid: str
    question: str
    ddl_context: DDLContext


class QueryIntent(ContractModel):
    query_type: Literal["detail", "aggregate", "ranking", "trend", "comparison"]
    measure_quotes: list[str]
    dimension_quotes: list[str]
    filters: list[FilterIntent]
    time_quote: str | None
    grain: Literal["day", "week", "month", "quarter", "year"] | None
    sorts: list[SortIntent]
    limit: int | None
    ambiguities: list[QueryAmbiguity]


class QueryDraft(ContractModel):
    sql: str
    params: dict[str, str | int | float | bool | None]
    table_ids: list[str]
    column_ids: list[str]
    metric_ids: list[str]


class QueryEvent(ContractModel):
    kind: Literal["clarification", "metadata", "rows", "complete"]
    sql: str
    columns: list[str]
    rows: list[list[object]]
    row_count: int | None
    elapsed_ms: int
```

HTTP adapter 使用 NDJSON `StreamingResponse`。`metadata` 事件先发送 SQL/columns，随后发送若干不超过 500 行或 1 MiB 的 `rows` 事件，最后发送 `complete`。澄清只发送一个 `clarification` 事件并完成 Conversation 轮次。这样不需要保存分页游标或把全部结果积存在应用内存中。

`QueryDraft` 是模型输出，不是可执行命令。只有通过全部静态规则后才能转换为 `ValidatedQuery`；executor 只接受 `ValidatedQuery`，从类型层阻止未校验 SQL 绕过门禁。

## 6. Query Context 构建

### 6.1 输入作用域

MVP 继续绑定当前 `source + DDL`，与现有 Chat 一致。`parse_ddl` 生成当前 schema 的稳定 table/column IDs 和 FK 边；这些 ID 构成 Meta 候选 allowlist。

该方式避开现有 Meta Search 没有 source filter 的缺口，也不需要修改索引 payload 或重建索引。独立于当前 DDL 的全局问数入口出现后，再给 Meta Projection 增加 source/schema filter。

### 6.2 召回顺序

1. 使用严格 structured output 把问题拆成 `QueryIntent`。所有 quote 必须逐字存在于当前或此前用户消息；不得补默认指标、时间或过滤条件。
2. 用完整问题调用一次 `search_metadata(query, kinds=None)`。
3. 丢弃 table/column/metric 归属不在当前 DDL allowlist 的候选。
4. 按 `table_id` 归并，指标的 `related_column_ids` 扩展字段范围。
5. 为每个 measure/dimension/filter quote 建立真实候选集合。只有唯一候选或明确用户业务规则才能自动绑定；模型 confidence 不作为放行依据。
6. 多候选、无候选或缺少必要事实表时，每轮按“指标口径 → 时间范围 → 维度 → 过滤 → 排序/数量”只返回一个最高影响澄清问题，不生成 SQL。下一用户轮次通过 Conversation 上下文重建完整 `QueryIntent`，保留已确认语义并重新校验。
7. 仅对保留的 column IDs 调用 `search_values`。
8. 用当前物理 schema 补齐数据类型、nullable、主键和 FK join edge。
9. 按固定字符/对象预算裁剪并生成 QueryContext。

字段值只能当 literal 候选。它是 top-N 派生投影，不是完整 distinct domain；`complete=false` 也不能据此判定用户输入不存在。

## 7. SQL 生成与校验

### 7.1 生成规则

模型必须以严格 structured output 返回 `QueryDraft`：

- SQL 只引用 QueryContext 中的 DW table/column。
- 用户值通过命名参数表达，不允许字符串拼接。
- 必须返回实际引用的 table/column/metric IDs。
- 只生成一条 MySQL `SELECT` 或以 `WITH` 开始并最终返回 `SELECT` 的语句。
- 默认包含结果上限；模型不能决定数据库、账号、超时或权限。

### 7.2 静态门禁

使用 `sqlglot.parse` 并要求仅一条 AST。必须同时满足：

1. 根语句是只读查询；拒绝 DML、DDL、管理命令、多语句和注释拼接。
2. schema 只能是 `dw`；拒绝 `mysql`、`information_schema`、`performance_schema` 等系统库。
3. table/column 必须属于 QueryContext allowlist。
4. 禁止 `SELECT *`、`CROSS JOIN`、未由当前 FK 边支持的 JOIN。
5. 禁止 `INTO OUTFILE`、`DUMPFILE`、`LOAD_FILE`、`SLEEP`、`BENCHMARK`、锁函数和用户变量。
6. predicate 中的用户值必须是绑定参数；参数集合与 SQL placeholder 完全一致。
7. 禁止模型擅自添加、删除或缩小用户明确的 Top-N/limit；没有业务 limit 的聚合和明细查询不得被固定 `LIMIT` 改变语义。
8. QueryDraft 声明的 ID 与 AST 实际引用一致。

### 7.3 数据库预检

静态校验通过后，用同一个专用 DW 只读连接执行 `EXPLAIN`。它负责发现不存在的表列、类型/函数错误和数据库解析问题，不返回业务行。

静态校验或 `EXPLAIN` 失败时，只把稳定错误码、对象名和允许修复的约束反馈给模型；不得把连接串、SQL driver 文本或内部状态送入模型。

### 7.4 有限修复

修复预算固定为一次：

```text
attempt 0 -> validate/explain failed -> repair once
attempt 1 -> validate/explain failed -> QUERY_UNSAFE
```

执行超时、权限拒绝、连接失败或数据不就绪不触发模型修复，因为它们不是 SQL 语义错误。

## 8. 自动执行门禁

用户已明确不需要确认。自动执行前必须全部满足：

| 门禁 | 最小要求 |
|---|---|
| 数据就绪 | 用 AST 实际 target tables 调用 Readiness；任一依赖未就绪时保持固定回复 `数据准备中，请稍后重试`。 |
| 数据库权限 | 使用独立 DW 查询账号，只授予 `SELECT`；不得复用当前可写应用 session。 |
| 事务 | 开启只读事务；executor 不暴露 commit/DML interface。 |
| 时间 | 单次执行上限 10 秒；超时取消并返回稳定错误。 |
| 结果传输 | 不限制业务结果总量。执行层每批最多读取/返回 500 行或 1 MiB，并提供稳定后续游标或流式批次；不能通过固定 `LIMIT` 截断总结果。 |
| 并发 | 复用连接池有界容量，不创建无界后台任务。 |
| 审计 | 记录 user/conversation/turn、SQL hash、table IDs、耗时、行数、结果状态；不记录参数值和业务行。 |

数据库账号是最终防线。即使 AST validator 有缺陷，最小权限账号也不能执行写操作或访问非 DW schema。

## 9. 失败与用户输出

| 场景 | 行为 |
|---|---|
| 非业务数据问题 | 交给现有 DDL Chat。 |
| 召回不足或语义歧义 | 返回一个具体澄清问题，不生成 SQL。 |
| 字段值投影不完整 | 继续使用结构元数据；不把候选值当完整约束。 |
| 数据未就绪 | 只返回 `数据准备中，请稍后重试`。 |
| 两次 SQL 校验均失败 | 返回“无法生成安全查询，请调整问题”。 |
| 执行超时/结果过大 | 返回有界错误或截断结果，不自动放宽预算。 |
| 数据库/模型不可用 | 返回稳定 502/503；不切换到未校验 SQL 或编造结果。 |

## 10. MVP 与后续增强

### MVP

- 当前 DDL/source 绑定。
- 统一 DW 全来源查询。
- 单次 Meta 召回，字段确定后再查值。
- DDL FK 关系补全。
- 一次生成 + 最多一次修复。
- SQLGlot + `EXPLAIN` + Readiness + 只读执行。
- 10 秒执行预算；明细结果以 500 行或 1 MiB 为单批传输预算，不限制可继续获取的总量。

### 仅在出现真实需求时增加

- 独立全局问数入口：为 Meta Projection 增加 source/schema filter 和关系持久化。
- 单来源查询：为统一 DW 设计确定性 owner join 或来源维度，不让模型猜 source 列。
- 可执行指标表达式：升级指标契约，保存结构化 aggregation/filter/grain，而不是继续解析自然语言 definition。
- 查询成本控制：采集 `EXPLAIN` 成本证据后再增加扫描行数或 cost 阈值。
- durable audit：有合规留存和检索要求时新增权威审计表。

## 11. 关键风险

1. **指标语义不是可执行公式**：当前指标只有自然语言 definition 与依赖列。MVP 必须在歧义时澄清；不能声称 AST 校验能证明业务口径正确。
2. **Meta semantic search 无完整性标志**：零结果可能是无匹配或投影未收敛。必须失败关闭，不能让模型绕过召回直接猜表。
3. **统一 DW 不含 source 列**：当前 source 只限定元数据上下文，不限定结果行来源。MVP 响应和文档必须明确“全来源”。
4. **自动执行扩大风险**：不能删除只读账号、资源预算和审计中的任何一项来缩短实现。
5. **批次不等于业务上限**：固定 `LIMIT` 既会改变结果语义，也不能阻止大表聚合扫描；查询成本依靠超时、并发、取消和后续有证据的 `EXPLAIN` 成本策略控制。

## 12. 仓库证据

- `backend/src/chat/service.py:32-36,59-190`
- `backend/src/answer_readiness/service.py:30-61`
- `backend/src/answer_readiness/tool.py:17-56`
- `backend/src/ddl_metadata/meta_projection/application/search.py:67-138`
- `backend/src/ddl_metadata/meta_projection/models.py:11-17,119-158`
- `backend/src/ddl_metadata/meta_projection/qdrant.py:142-203`
- `backend/src/ddl_metadata/meta_projection/projections.py:414-582`
- `backend/src/models/physical.py:10-58`
- `backend/src/models/semantic.py:130-145`
- `backend/src/data_sync/models.py:63-101`
- `backend/src/infrastructure/mysql.py:29-84`
- `backend/pyproject.toml:27`
- `docs/agent-knowledge.html:163-189`
