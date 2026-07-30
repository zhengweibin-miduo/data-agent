# 同步会话元数据到 DW

## Goal

根据 LLM 会话中已经确认的表、字段和指标元数据，在 `dw` 数据库异步创建或演进对应的表和列，并将源业务库数据增量同步到目标表；元数据定义继续由 `meta` 数据库管理。

## Background

- 普通对话会话当前只持久化消息、摘要和长期记忆上下文，不包含表、字段或指标字段。
- 表信息、字段信息和指标信息实际保存在 DDL LangGraph checkpoint 的 `physical_schema`、`semantic_metadata` 和 `metrics` 等状态中。
- DDL 工作流现有唯一成功出口是 `persist_snapshot`，其调用 `MetadataSnapshotService.persist`，再通过 `MetadataRepository.synchronize` 写入 MySQL。
- 当前 MySQL 配置默认数据库为 `meta`，目标表为 `table_info`、`column_info`、`metric_info` 和 `column_metric`，该库只负责元数据持久化。
- 本任务目标是独立的 `dw` 数据库；项目建库约定为 `CREATE DATABASE IF NOT EXISTS dw DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci`。
- `dw` 当前仅由 Docker 初始化 SQL 创建并写入样例数据，包含 `dim_region`、`dim_customer`、`dim_product`、`dim_date` 和 `fact_order`。
- 应用运行时只有一个指向 `meta` 的 MySQL URL，没有 `dw` 连接、运行时表模型、repository 或 service。
- DDL 请求中的 `source` 只是来源标识，当前没有源业务库连接配置、凭证模型、在线表结构探测或数据查询能力。
- 项目尚无 ETL、批量复制、CDC、增量游标或动态目标表命名契约。
- 当前缺口不是 Meta 快照事务失败，而是应用尚未实现运行时 DW 数据同步能力。
- 当前物理模型能从 DDL 确定主键；增量同步改用 MySQL Binlog CDC，不再依赖业务增量时间列。
- 需要基于现有会话与元数据链路定位唯一、最小的同步边界，不新增重复模型或旁路流程。

## Requirements

- 表、字段和指标定义继续持久化到 `meta`，不得复制到 `dw`。
- 同步对象是源业务库实际表数据，不是 LLM 查询结果集。
- DW 表结构就绪后，同一异步同步链路继续增量复制源业务库实际表数据。
- 使用会话确认后的表信息和列信息在 `dw` 创建不存在的表和列，并按明确的兼容策略修改已有结构。
- 自动结构演进只允许创建缺失表、添加缺失列和安全扩大字段类型。
- 删除列、重命名列、缩窄字段类型及其他可能丢失数据的变化不得自动执行；同步必须失败并报告结构差异。
- 指标信息用于确认指标依赖列完整，不在 `dw` 中创建 Meta 指标定义表。
- 每张待同步表必须在 DDL 中声明主键；缺少主键时拒绝该 DDL Job，不生成 Meta 快照，也不触发 DW 同步。
- 支持多个命名数据源；DDL 请求中的 `source` 用作服务端数据源配置键。
- 数据源地址、账号和密码只保存在服务端配置中，不进入 API 请求、LLM 上下文、Redis checkpoint 或日志。
- DW 表名由会话确认的统一业务元数据决定，不添加数据源前缀，也不按来源拆分事实表。
- `source` 仅用于后续数据同步时选择源连接并标识同步任务来源。
- 同一 DW 事实表允许多个命名数据源并发写入；`data_sync` 按 `(source, target_table)` 独立维护任务、回填进度和 Binlog 位点。
- `data_sync` 维护 `(target_table, primary_key) -> source` 的目标主键归属；首次成功写入确定归属。
- 其他数据源写入已归属的相同目标主键时必须进入冲突状态，不得覆盖 DW 现有行或推进对应 Binlog 位点。
- DW 数据同步使用独立连接和清晰的数据落表边界，不复用 Meta repository。
- Meta 快照成功后异步触发 DW 结构同步；Meta 快照是异步任务的权威输入。
- DDL Job 的成功只取决于 Meta 快照成功，不等待 DW 建表完成。
- DW 结构同步失败不得回滚 Meta 快照或改变已成功的 DDL Job；异步任务必须支持幂等重试。
- 增量数据同步使用 MySQL Binlog CDC，覆盖 INSERT、UPDATE 和 DELETE，并按源表主键幂等应用到 DW。
- 首次历史数据同步不得使用整表长事务或无界一次性扫描，避免对大表源库造成持续 I/O 和 MVCC/undo 压力。
- 首次历史数据同步必须按主键分块、限速、可暂停并可从已完成分块续传。
- 首次同步先记录源 Binlog 位点并暂存后续事件，再按主键分块回填历史数据；回填完成后按位点顺序回放暂存事件，追平后切换为实时 CDC。
- 回填期间暂存的 Binlog 事件保存在 `data_sync`，追平并确认位点后可清理。
- 新建独立的 `data_sync` MySQL 数据库，专门保存同步任务、Binlog 位点、批次状态、重试次数和错误信息。
- `data_sync` 不保存 DW 业务行数据；`dw` 不保存同步游标、任务状态或重试记录。
- 同步失败必须可观测，且不得静默丢失数据。
- 不改变现有 LLM 会话的对外接口和正常响应语义。

## Acceptance Criteria

- [ ] 一次包含表、字段和指标信息的有效 LLM 会话完成后，`dw` 中存在对应表及所需列。
- [ ] 重复处理同一份元数据不会重复创建表或列，最终结构保持一致。
- [ ] 指标引用的列在 DW 目标结构中存在；依赖缺失时阻止成功并报告明确原因。
- [ ] 任一待同步表的 DDL 未声明主键时，DDL Job 明确拒绝且不产生同步任务。
- [ ] 首次同步建立一致性基线及对应 Binlog 位点，后续只消费该位点之后的变更。
- [ ] 大表首次同步使用有界批次，批次大小和间隔可配置；中断后不从头复制。
- [ ] 同一增量批次重复执行不会在 DW 产生重复行。
- [ ] Binlog 位点只在对应事件或批次成功写入 DW 后推进。
- [ ] 源表 INSERT、UPDATE 和 DELETE 均能在 DW 正确体现。
- [ ] 多个数据源可并发写入同一事实表；跨源主键碰撞被记录为冲突且不会覆盖已有 DW 行。
- [ ] 同步失败时保留明确日志或错误状态，可定位失败阶段和原始原因。
- [ ] 现有会话保存与元数据相关测试继续通过，并新增最小可运行检查覆盖 DW DDL 同步链路。

## Out of Scope

- 未经证据证明必要，不新建第二套表、字段或指标领域模型。
- 不在本任务中扩展新的 LLM 会话能力或检索能力。

## Operational Scope

- 首版状态、重试、死信和跨源主键冲突通过 `data_sync` 表、结构化日志及告警暴露，不新增公开查询接口。
