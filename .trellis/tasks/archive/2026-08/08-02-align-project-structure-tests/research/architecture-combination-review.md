# 项目结构组合 Skill 复审

## 方法与范围

- `codebase-onboarding` Phase 1：核验 manifest、技术栈、源码根、运行入口、worker/CDC、构建部署、CI 与测试布局。
- `codebase-onboarding` Phase 2：追踪 DDL Job、Conversation/Long-term Memory、Data Sync/Metadata Indexing、前端 HTTP/SSE 的请求流与数据流。
- `codebase-design`：以 module、interface、seam、adapter、depth、leverage、locality 判断职责与依赖方向。
- `domain-modeling`：识别 `metadata_indexing` 上下文归属缺口；结论尚未由用户确认，因此未修改 `CONTEXT.md`。
- `tdd` + `verification-before-completion`：将架构问题映射到公共测试 seam，并定义完成证据门禁；本次未运行全量质量命令，不声称测试通过。

## 总结论

1. **前后端源码根与运行边界总体符合规定。** `frontend/` 是独立 React/Vite 应用，FastAPI 默认 API-only，`src/data_agent/frontend/` 仅通过显式 legacy 开关挂载；未发现跨端源码导入。
2. **后端是模块化单体，但只局部、渐进式采用 DDD，不能判定为全面符合 DDD。** 多个 application module 直接依赖 MySQL/Redis/ES/Qdrant/TEI、具体 repositories 与全局配置；不同 bounded context 直接共享表和 repository implementation。
3. **没有发现 Python 解释器级导入环，但存在结构性依赖环。** 最明确的是 `data_sync.backfill → metadata_indexing.value_refresh → data_sync.models/tables`。
4. **DDD 规则本身大部分清晰，但 context map 不完整。** `chat`、`metadata_indexing` 未进入 backend Ownership/Dependency Direction；尤其 Metadata Projection 的领域归属未定义。
5. **测试臃肿与架构 seam 缺失是同一问题的两面。** Data Sync、Metadata Indexing 和 Workbench 测试之所以越过 interface，是因为生产 module 没有提供足够稳定且可注入的 seam。

## 真实架构地图

### 运行形态

- Python 3.13 模块化单体：FastAPI API 入口、arq DDL worker、独立 CDC worker，共用 MySQL/Redis/ES/Qdrant/TEI 基础设施。
- React/Vite 前端独立构建部署，经 HTTP/SSE 与 FastAPI 交互。
- MySQL 保存 Meta Snapshot、Conversation、Long-term Memory、Data Sync task/outbox 等权威状态；Redis Job Hash 是 DDL Job 权威运行状态，Redis Stream 是通知日志；ES/Qdrant 是可重建 projection。

### 请求与数据流

- DDL：HTTP submit → `DDLJobStore`/Redis CAS+outbox → arq worker → LangGraph → accepted snapshot transaction → Meta/Memory/Data Sync desired/Metadata Projection outbox → Job state/SSE。
- Conversation：HTTP/chat → Conversation turn transaction → context assembly → Long-term Memory recall → assistant completion + extraction outbox → Memory authoritative write + projection outbox。
- Data Sync：accepted desired state → schema sync → binlog buffer/backfill/replay/streaming → DW rows/readiness。
- Metadata Projection：accepted Meta desired + Data Sync DW peer state → outbox dispatcher → Qdrant semantic projection / ES value projection → authoritative Meta readback。

## DDD / Ports-and-Adapters 符合矩阵

| Area | 符合项 | 不符合或未定义项 | 判定 |
|---|---|---|---|
| DDL Metadata | HTTP、Redis、worker、LangGraph、snapshot interface 分工清楚；graph dependencies 可注入 | `MemoryContextLoader` 直接访问 MySQL；`MetadataSnapshotService` 在 persistence 包内编排四个 context 的具体 repositories | 部分符合 |
| Long-term Memory | `memory.domain` 有确定性规则；MySQL authoritative / ES-Qdrant projection 分离清楚 | application 直接依赖 infrastructure、具体 repository、全局 config；`models.memory` 同时承载领域内容、候选、HTTP/search contract | 最接近 DDD，但仍不完整 |
| Conversation | Conversation/turn/outbox 权威状态和事务不变量清楚 | application 直接依赖 MySQL/repository；跨 context 直接调用 `MemoryRepository`，没有 Memory port/防腐层 | 不符合严格依赖规则 |
| Data Sync | task phase、coordinate、desired state 形成明确领域状态机 | `DataSyncService` 直接依赖 source、repository、schema adapter、MySQL；`backfill` 反向调用 Metadata Projection implementation | 部分符合，存在结构环 |
| Metadata Projection | desired/outbox/generation/rebuild/search 业务行为真实且复杂 | bounded context 归属未定义；pure policy、SQL、repository、ES 和跨 context 表读取混在同包/大文件；被 Data Sync 反向调用 | 当前无法判定合规，必须先定归属 |
| Chat / Answer Readiness | Chat 通过构造参数接收 Conversation/readiness/model；用户行为编排明确 | Chat 直接使用 DDL parser；readiness tool 直接依赖 Data Sync repository/MySQL；规范未描述这两个 context 关系 | 渐进式，边界未完整建模 |
| Frontend | app shell、feature、API/SSE adapter、backend authority 分工符合 feature-first | Workbench 一个页面持有多个状态机；URL ownership 在 shell/feature 间镜像；测试越过 adapter interface | 总体符合，需深化内部 module |

## 关键证据与优先级

### P0：先定义 Metadata Projection 的领域归属

- `metadata_indexing/desired.py:6-8,209-233` 直接读取 Data Sync task 与 DDL column tables。
- `data_sync/backfill.py:28-32` 反向调用 Metadata Projection desired/value-refresh implementation。
- `ddl_metadata/persistence/snapshots.py:21-34,155-197` 又在 accepted snapshot transaction 直接编排 Metadata Projection repository。

在没有 context 归属前，无法判断哪些导入应保留为同 context 内部 adapter、哪些必须改成 port/event/anti-corruption layer。

已确认模型：`metadata_indexing` 属于 DDL Metadata 拥有的 **Meta Projection**（Meta Snapshot 的可重建搜索表示），不是独立业务 bounded context。Data Sync 只提供值投影所需的稳定 read port/projection event，不反向调用 Meta Projection implementation。统一语言与 context 关系已记录到 `CONTEXT.md`、`CONTEXT-MAP.md`。

### P0：清除 application → infrastructure 直接依赖

- `memory/application/service.py:11,43,124...` 直接使用 `MySQLDatabase` 与 `app_config`。
- `memory/application/search.py:11-14,36,76...` 直接使用 MySQL/ES/Qdrant/TEI。
- `conversation/service.py:15-22,35...` 直接使用 Conversation/Memory repositories、MySQL 和 config。
- `data_sync/service.py:13-32,53-63` 直接依赖 backfill/binlog/schema/repository/MySQL。
- `answer_readiness/tool.py:9-12,25-35` 直接依赖 Data Sync repository/MySQL/config。

应把事务、repository/source/index/config 所需值定义为 application ports 或构造输入，由 composition root 选择 adapters。不要为每个类机械创建接口；只有生产 adapter + in-memory/test adapter 或真实变化存在时才建立 seam。

### P1：拆开跨 context 事务协调与具体持久化

`MetadataSnapshotService.persist` 的 interface 很小且行为很深，但 seam 放错位置：实现位于 `ddl_metadata.persistence`，却直接协调 Meta、Memory、Data Sync、Metadata Projection 四类具体 repositories。原子提交是必须保留的业务不变量，但应由明确的 accepted-snapshot publication module/transaction adapter 承担，并通过各 context 的 publication port 协作。

### P1：领域模型与跨层 ContractModel 混用

`memory/domain/candidates.py:16-30` 的领域规则直接使用 `data_agent.models.*`；`models/base.py:6-9` 说明这些对象是 Pydantic `ContractModel`。`models/memory.py` 同一文件同时包含领域内容、`MemoryCandidate`、`MemoryDetail`、`MemorySearchResponse`。这使 domain model、application command/result 和 HTTP/serialization contract 没有唯一所有者。

不要求把所有 Pydantic 全部删除；应先区分真正的 domain value/entity、application command/result 和 transport projection，只迁移本轮触及的 interface。

### P1：深化高耦合 module

- `MetadataSnapshotService.persist`：已有 depth，但跨 context adapter 选择泄漏，需移动 seam。
- `MetadataValueRefresh`：文件超过 1,400 行，并向 Data Sync 暴露多个 helper；外部 interface 过宽、locality 差。
- `WorkbenchPage`：外部 props interface 很小，不能仅因 522 行判定浅；问题是内部 restore/submission/SSE/chat/clarification 状态机没有稳定 internal seams，测试因此直接驱动 callback implementation。
- `DDLJobStore` 和前端 `apiRequest`/`jobEvents` 是现有较好的深 module：小 interface 隐藏 Redis/Lua 或 HTTP/SSE 复杂度，应保留其 leverage。

### P2：修正规范漂移

- backend directory spec 的“backend-only”与独立前端现实冲突。
- Layout/Ownership/Dependency Direction 遗漏 `chat`、`metadata_indexing`、`memory/versions.py` 等现有内容。
- 规范更新必须记录目标 context 和依赖矩阵，不能简单复制当前目录树为“规范”。

## 前后端复审结论

- `frontend/` 和 `src/data_agent/` 的物理所有权符合规定。
- FastAPI 默认不读取 Vite 源码或构建产物；legacy 静态资源是明确允许的迁移兼容，不应误删。
- 前端所有 HTTP/SSE 经过 `frontend/src/api`；未发现前端导入 Python/ORM/内部 DTO。
- 当前跨端契约权威仍是 Pydantic，前端 validators/types 是手工投影；本轮若改变契约，必须先改后端权威，再更新客户端投影和两端测试。

## 测试组合结论

详细 seam 与替换策略见 `research/test-seam-map.md`。

- 不能按“文件大”批量删测试；先确认 DDL lifecycle、accepted snapshot、Conversation/Memory、Data Sync lifecycle、Metadata Projection、frontend transport/feature 六类公共 seam。
- 优先替换直接调用 `_process/_capture/_synchronize`、直接驱动 `connectJobEvents.mock.calls`、以及重复验证 adapter 机制的测试。
- 当深 module interface 的新测试存在后，删除旧浅层测试，执行 replace-don't-layer；不能新旧两套叠加。
- LLM repair 预算、幂等调用上限、事务/锁顺序等若本身是外部契约，call-count/order 仍可在对应 adapter contract test 中保留。

## 用户决定与待确认项

1. **已确认**：`metadata_indexing` 定义为 DDL Metadata context 内的 **Meta Projection**。
2. **已确认**：六类公共测试 seam 作为后续测试重构的新增/保留边界；新 interface 覆盖后执行 replace-don't-layer。
