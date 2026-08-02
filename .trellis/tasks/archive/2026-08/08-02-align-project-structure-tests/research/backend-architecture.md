# 后端模块职责与依赖取证

扫描范围：`src/data_agent/**/*.py`；对照 `.trellis/spec/backend/directory-structure.md` 及根 `AGENTS.md` 的后端分层规则。以下仅记录代码事实与可直接核验的影响。

## 已核验的符合项

- `memory.domain` 仅导入根模型、标识符及同域纯函数：`memory/domain/payloads.py:6`、`lifecycle.py:3`、`policies.py:6`、`candidates.py:9-25`。未发现 FastAPI、SQLAlchemy session、Redis、ES、Qdrant、TEI 或 initialized client 导入，符合目录规范对 deterministic domain 的限制。
- `conversation` 包未发现 `from data_agent.ddl_metadata...` 或 `import data_agent.ddl_metadata`。其服务通过 `memory.application.search`、`memory.mysql.repository` 使用根 memory（`conversation/service.py:15-21`），符合“Conversation 不反向依赖 DDL”约束。
- 共享 SQLAlchemy `MetaData` 定义为单例 `data_agent.persistence.schema.metadata`（`persistence/schema.py:3-5`）；Conversation、memory、data_sync、DDL snapshot tables 均从该模块导入（如 `conversation/mysql_tables.py:18`、`memory/mysql/tables.py:18`、`data_sync/tables.py:18`、`ddl_metadata/persistence/tables.py:11`）。
- 组合根集中在 `data_agent.application`：资源初始化和业务服务挂载位于 `application.py:43-79`，并注入 `DDLJobStore` 与 `MetadataMemoryReferenceValidator`（`application.py:70-78`）。

## 真实分层/边界问题

### 1. application 层直接依赖 infrastructure 具体客户端

`memory.application.service` 直接导入 `data_agent.infrastructure.mysql.MySQLDatabase`（`memory/application/service.py:11`）及全局 `app_config`（`:43`）；`memory/application/search.py` 直接导入 `ElasticsearchClient`、`MySQLDatabase`、`QdrantClient`、`TEIEmbeddingClient`（`memory/application/search.py:11-14`）和 `app_config`（`:36`）。

事实影响：按根规则，application 应负责用例编排、事务边界和抽象 driving/driven ports，只依赖领域模型与抽象端口；外部资源应由 adapters/infrastructure 和组合根选择。当前 memory application 将基础设施生命周期/配置类型作为具体依赖，降低可替换性并使内层直接指向外层。

### 2. metadata_indexing 未在当前 directory-structure 规范中登记，且跨 bounded context 直接耦合持久化表

实际存在 `src/data_agent/metadata_indexing/`（`dispatcher.py`、`desired.py`、`projections.py`、`value_refresh.py` 等），但 `.trellis/spec/backend/directory-structure.md:15-116` 的布局没有该 bounded context。

更具体地，`metadata_indexing/value_refresh.py:29-36` 从 `data_sync.models`、`data_sync.tables` 及 `ddl_metadata.persistence.tables.column_info` 导入；`metadata_indexing/projections.py:12-15` 同时导入 `data_sync.models/tables` 与 `ddl_metadata.persistence.tables`；`metadata_indexing/desired.py:6-8` 也直接导入这两类表/模型。事实影响：metadata_indexing 的实现直接绑定 data_sync 与 DDL 的 ORM/Core 表，而非经由明确端口；变更任一 bounded context 的表结构会穿透到索引模块。该耦合与规范“不同 bounded context 默认不直接共享实体或 ORM 模型、通过标识符/领域事件/防腐层协作”的规则冲突。是否为计划中的新增上下文需结合任务目标确认。

### 3. DDL workflow contracts 反向导入 workflow.memory_context

`ddl_metadata/workflow/contracts.py:6` 导入 `LoadedMemoryContext`（`workflow/memory_context.py`）。这不是跨 bounded context，但使本应可独立导入的 contracts 依赖 memory-context 实现模块；规范要求 `workflow.state` 和 `.contracts` 可在无 graph 构造时导入。当前导入链需核验 `memory_context.py` 的依赖（其本身导入 MySQL、memory repository 等，`workflow/memory_context.py:11-17`），因此 contracts 的可独立性存在风险；未执行运行时 import 测试，不能断言已失败。

## 其他跨包调用链取证（非直接违规结论）

- 组合根把 DDL reference adapter 注入 root memory：`application.py:21-23,70-73`；memory application 本身未导入 `ddl_metadata`，符合依赖倒置意图。
- DDL workflow nodes 使用 root memory domain（`ddl_metadata/workflow/nodes.py:18-19`），DDL snapshot persistence 直接使用 `memory.mysql.repository`（`ddl_metadata/persistence/snapshots.py:19-20`）；这是 DDL 作为外层适配/事务组合者的现状，但同时使 DDL persistence 依赖 memory 的具体 MySQL repository。
- `chat.service` 直接调用 `ddl_metadata.parsing.parse_ddl`（`chat/service.py:28`），说明 chat bounded context 与 DDL parsing 存在运行时耦合；该关系不在当前目录规范的 bounded-context 列表中（chat 包也未列于规范布局），需判断是否属于迁移期代码或遗漏规范。

## 未覆盖/存疑

- 未执行完整 import graph、Ruff/Pyright 或运行时测试；因此无法确认 contracts 的“可独立导入”是否实际报错。
- 规范目录布局与当前仓库存在明显漂移（`chat`、`metadata_indexing`、`memory/versions.py`、DDL redis 新模块等），本报告只指出事实，不判断这些目录应删除、迁移还是补写规范。
