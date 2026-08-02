# src/data_agent 导入图审查

范围：`src/data_agent/**/*.py`（124 个文件），按 AST 收集 `data_agent.*` 导入。模块图未发现有向环（DFS 无回边）；但存在大量层次倒置和 bounded context 直接耦合。

## 事实与最短导入链

### 1. Domain/application → adapters/infrastructure（Ports-and-Adapters 违规）

- `memory.application.service.MemoryService` 直接导入 `data_agent.infrastructure.mysql.MySQLDatabase`（`memory/application/service.py:11`），并导入具体 `memory.mysql.repository.MemoryRepository`（:20-23）。最短链：`memory.application.service → infrastructure.mysql`；`memory.application.service → memory.mysql.repository`。应用层因此持有驱动和持久化实现，seam（`MemoryMutationLeaseProvider`/`MemoryReferenceValidator` Protocol，:46-64）未覆盖数据库/仓储边界。
- `memory.application.search.MemorySearchService` 直接导入 Elasticsearch/MySQL/Qdrant/TEI 客户端（`memory/application/search.py:11-14`）及 indexing 实现（:21-28）；最短链：`memory.application.search → infrastructure.elasticsearch`。这是 application→adapter，降低 module depth 与 locality。
- `memory.domain.candidates`（领域层）直接依赖共享 `models.memory/physical/semantic`（`memory/domain/candidates.py:16-30`）。这些模型包含跨上下文契约/持久化语义，领域规则不能独立演化；最短链：`memory.domain.candidates → models.memory`。

### 2. bounded context 跨域直接导入实现/表/仓储

- `metadata_indexing.value_refresh` 直接导入 `data_sync.models`、`data_sync.tables`、`ddl_metadata.persistence.tables`、基础设施 MySQL/Elasticsearch 及 metadata 自身 repository/tables（`metadata_indexing/value_refresh.py:29-64`）。最短跨域链：`metadata_indexing.value_refresh → data_sync.tables`；`metadata_indexing.value_refresh → ddl_metadata.persistence.tables`。
- `metadata_indexing.desired` 直接导入 `data_sync.models` 与 `data_sync.tables`（`metadata_indexing/desired.py:6-7`），并导入 `metadata_indexing.repository`（:15）；最短链：`metadata_indexing.desired → data_sync.tables`。
- `metadata_indexing.projections` 同时导入 `data_sync.models/tables`、`ddl_metadata.persistence.tables`、`metadata_indexing.repository/tables`（`metadata_indexing/projections.py:12-32`）；最短链：`metadata_indexing.projections → data_sync.tables`。
- `data_sync.backfill` 直接调用 metadata 实现 `metadata_indexing.desired` 与 `metadata_indexing.value_refresh`（`data_sync/backfill.py:28-29`）；最短链：`data_sync.backfill → metadata_indexing.value_refresh`。与上一组形成双向 bounded-context 耦合（无 Python 环，但结构环）：`data_sync.backfill → metadata_indexing.value_refresh → data_sync.models/tables`。
- `ddl_metadata.persistence.snapshots` 直接组合 `data_sync.repository`, `memory.mysql.repository`, `metadata_indexing.repository`（`ddl_metadata/persistence/snapshots.py:8-31`）；最短链：`ddl_metadata.persistence.snapshots → memory.mysql.repository`。该 persistence 模块成为跨域实现汇聚点而非端口适配器。
- `conversation.extraction` 直接依赖 `memory.mysql.repository`（`conversation/extraction.py:32`）及 infrastructure MySQL（:22）；`conversation.service` 直接依赖 `memory.application.search` 和 `memory.mysql.repository`（`conversation/service.py:19-20`）。最短链：`conversation.service → memory.mysql.repository`。
- `answer_readiness.tool` 直接依赖 `data_sync.repository`（`answer_readiness/tool.py:9-10`）和 infrastructure MySQL（:11）；最短链：`answer_readiness.tool → data_sync.repository`。

### 3. API/组合根边界

- `data_agent.application` 同时装配 API、DDL jobs store、各基础设施客户端及 memory application（`application.py:13-38`）。作为 composition root 这是合理的 adapter 选择，但它还把 `ddl_metadata.jobs.store`、`memory.application.service` 的具体实现暴露在单文件；应保持其唯一组合根职责。
- `ddl_metadata.worker.lifecycle` 直接导入 conversation extraction、memory indexing 实现、metadata indexing 实现及九个基础设施客户端（`ddl_metadata/worker/lifecycle.py:10-42`），属于 worker→多个 bounded context adapters 的高耦合 module。

## 按 module/interface/seam/adapter/depth/leverage/locality 分类

- **module**：`memory`, `data_sync`, `metadata_indexing`, `ddl_metadata`, `conversation` 均有清晰目录，但跨域模块通过具体 `models/tables/repository` 相连，边界名义存在、接口边界缺失。
- **interface/seam**：可见 Protocol 仅在 `memory/application/service.py:46-64`（租约与引用校验）；实际 `MySQLDatabase`、`MemoryRepository`、搜索/indexing 客户端均以具体类型注入，seam 未形成可替换端口。
- **adapter**：`infrastructure/*`、`memory/mysql/*`、`metadata_indexing/*tables/repository`、`data_sync/tables/repository` 是驱动/持久化适配器，却被 application、worker 和其他 bounded context 直接导入。
- **depth**：`memory.domain` 的 domain depth 被 `models.*`（含 Pydantic DTO/跨域契约）拉浅；`memory.application`、`metadata_indexing.value_refresh` 深度不足，因同时编排 SQLAlchemy 表和基础设施客户端。
- **leverage**：`data_agent.models.*`、`persistence.schema`、`settings` 被几乎所有上下文复用，杠杆高但成为共享内核；跨域表/仓储导入使变更半径扩大。
- **locality**：同一用例的规则、事务、持久化分散在 context 外（例如 metadata value refresh 横跨 data_sync、ddl_metadata、infrastructure），导致修改需多目录同步。

## 结论（仅审查事实）

未检测到 Python 导入环；主要风险是结构性依赖环和层次倒置，而非解释器级循环。最短高优先级路径：

1. `memory.application.service → infrastructure.mysql` / `memory.mysql.repository`；
2. `metadata_indexing.value_refresh → data_sync.tables` / `ddl_metadata.persistence.tables`；
3. `data_sync.backfill → metadata_indexing.value_refresh`（与 2 构成跨上下文双向耦合）；
4. `ddl_metadata.persistence.snapshots → memory.mysql.repository`；
5. `conversation.service → memory.mysql.repository`。
