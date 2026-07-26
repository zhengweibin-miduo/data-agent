# Schema 与根级现行说明审查

## 审查文件清单

- `docs/docker/mysql/data_agent.sql`（长期记忆、会话、消息、提炼任务、事件、关联、索引 outbox 的表/字段 COMMENT）
- `docs/docker/mysql/meta.sql`（`table_info`、`column_info`、`metric_info`、`column_metric`）
- `docs/docker/mysql/dw.sql`（地区、客户、商品、日期维度及订单事实）
- `src/data_agent/ddl_metadata/memory/mysql/tables.py`（长期记忆 SQLAlchemy 表定义）
- `src/data_agent/ddl_metadata/persistence/tables.py`（Meta 快照 SQLAlchemy 表定义）
- `src/data_agent/ddl_metadata/models/memory.py`（Pydantic 长期记忆领域契约）
- `src/data_agent/ddl_metadata/memory/mysql/repository.py`（活动槽、访问计数、状态/事件写入语义）
- `src/data_agent/conversation/mysql_tables.py`、`src/data_agent/conversation/repository.py`（会话/消息持久化与删除语义）
- 根级 `README.md`、`AGENTS.md`、`code_review.md`

## P0/P1 候选

未发现。SQL COMMENT 与对应 SQLAlchemy 列集合、类型宽度和模型字段一致，未能证明会造成重要功能错误、数据不一致或服务不可用。

## 非阻塞候选（维护建议）

未形成可确认缺陷。`data_agent.sql:111` 的 `event_type COMMENT '历史事件类型，如新增、更新、删除或关联'` 未枚举模型中的 `MERGE`、`NOOP`、`EXPIRE`，但使用“如”明确表示示例而非封闭枚举；`MemoryEventType` 在 `src/data_agent/ddl_metadata/models/memory.py:83-92` 定义了这些值，故不应作为问题发布。`agent_memory.active_key`（`data_agent.sql:18`）说明“同一作用域类别事实唯一活动槽”，仓储在 `repository.py:74-84` 以 `source/user_id/category/memory_key` 组成哈希，语义吻合。

## 已核验无问题项

- `data_agent.sql:12-48` 的长期记忆字段 COMMENT 与 `memory/mysql/tables.py:21-70` 列及 `MemoryDetail`（`models/memory.py:239-269`）的状态、版本、访问计数、生命周期字段一致。
- `data_agent.sql:52-81` 会话/消息表的用户、会话、轮次和文本字段与 `conversation/mysql_tables.py` 及仓储删除 API 的关系一致；“永久保存”描述持久化性质，不否认显式删除接口。
- `data_agent.sql:85-105` 提炼任务 COMMENT 与仓储中的领取/重试字段（`attempts`、`available_at`、lease）一致。
- `data_agent.sql:109-149` 事件、关联、索引 outbox COMMENT 与 `memory/mysql/tables.py:72-145` 及 `MemoryEvent`/`MemoryLink`/索引模型字段一致。
- `meta.sql:10-48` 与 `persistence/tables.py:13-47` 字段、类型和多对多主键一致。
- `dw.sql` 维度/事实表 COMMENT 与其字段定义及业务命名一致，未发现可由仓库代码证明的错误契约。
- `README.md:1-4` 仅作项目定位，无过期运行指令；`AGENTS.md:23-35` 的审查/Git 规则与 `code_review.md:5-20` 内容一致。

## 排除与限制

- 按任务要求排除 `.trellis/workspace/**`、历史任务、`.agents/skills/**`、`.codex/**` 等过程/工具内容。
- 未进行外部数据库实例或生产数据核验；结论基于当前 SQL、SQLAlchemy、Pydantic、仓储和根级说明静态证据。
- 本分区未修改产品文件。
