# Design

## 页面边界

保留 `docs/agent-knowledge.html` 的静态页面形态、现有深色技术图示语言、响应式目录和滚动高亮。页面面向第一次接触该项目的研发人员：不回避专业术语，但必须先建立问题、处理过程和结果之间的因果关系，再提供路径与状态作为核验入口。

每个核心章节统一采用以下阅读结构：

1. `解决的问题`：指出没有该机制时会出现的具体错误或一致性风险。
2. `示例`：给出输入、前置状态、处理步骤与可观察结果。
3. `机制`：详细解释组件职责、关键判断、事务或异步边界。
4. `边界`：明确当前未实现的能力和不能从机制推出的结论。
5. `代码定位`：保留真实路径、状态名与关键符号。

## 内容结构

1. 当前能力边界：先说明项目做什么，以及 readiness 不等于 DW 查询。
2. 架构总览：引用自包含 SVG，展示 Browser、API、DDL Worker、CDC、权威 MySQL、Redis 与两类投影。
3. Agent 主流程：Workbench → Chat HTTP → DDL 解析 → Conversation turn → Readiness → DDL Copilot → 原子完成轮次。
4. DDL Job：预览、受理、dispatch、LangGraph、interrupt/resume、终态/SSE。
5. Accepted Snapshot、Data Sync 与 Meta Projection：权威事务和异步派生链路。
6. Long-term Memory 与 Conversation：权威存储、混合检索、投影、turn lease 和证据抽取。
7. 模块地图与运行边界：当前路径、module/interface/seam/adapter、API/worker/CDC 启动职责。

## 图示体系

- `agent-system.svg`：系统总览，回答“有哪些运行进程、权威数据与派生投影”。
- `chat-turn-sequence.svg`：时序图，回答“一次 Chat 请求如何经过 Conversation、Readiness 与模型并原子完成”。
- `ddl-job-lifecycle.svg`：流程/状态图，回答“DDL Job 如何受理、澄清、恢复并终结”。
- `snapshot-convergence.svg`：数据流图，回答“Accepted Snapshot 提交后，Data Sync 与两类 Projection 如何独立收敛”。
- `conversation-memory.svg`：流程图，回答“一句话如何成为有证据、可修正、可检索的长期记忆”。
- 五张图共享颜色语义：用户/前端为 cyan，应用服务为 emerald，权威存储为 violet，异步协议为 orange，门禁/异常为 amber/rose。
- 所有图保存为独立自包含 SVG，并生成同名 `@2x.png` 作为视觉校验产物。页面复用一个原生 `<dialog>` 查看器，为每张图提供右上角四角展开、中心缩放与抓取平移。

## 示例边界

- Chat/Readiness：对比“字段含义”与“昨天销售额”两类问题；后者即使 Data Sync 已就绪，当前 Chat 也没有 SQL/DW 查询工具，不能给出精确数值。
- DDL 澄清：以含义不明确的 `status` 字段为例，解释 `waiting_input`、`question_set_id` 与同一 checkpoint 恢复。
- Snapshot/同步：以字段类型或语义变更为例，解释 Meta 事务先成功、CDC/投影失败独立重试而不反转 DDL Job。
- Conversation/Memory：以用户确认指标口径为例，解释用户原话证据、extraction outbox、candidate、投影检索和 MySQL 回查。

## 兼容与回滚

- 无运行时兼容问题；页面和图示都是文档资产。
- 回滚只需恢复 `docs/agent-knowledge.html` 并删除本任务新增图示资产。
