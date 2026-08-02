# Accepted Snapshot 与 Meta Projection 边界设计

## Purpose

本子任务把 Meta Projection 明确收回 DDL Metadata bounded context，并围绕两个高杠杆 seam 重构：accepted snapshot publication 与 Meta Projection lifecycle/search。目标不是为现有文件机械套四层目录，而是形成少量深模块，使事务原子性、派生投影收敛和权威回读成为稳定公共行为，外部存储与框架细节留在适配器中。

## Current Evidence

- `ddl_metadata.persistence.snapshots.MetadataSnapshotService` 同时构造 Meta、Memory、Data Sync 与 projection repositories，但它所保留的 generation lock + 单 MySQL transaction 是必须保留的业务不变量。
- `metadata_indexing.desired` 同时包含纯版本策略和对 `data_sync_task`、`column_info` 的 SQL 查询。
- `metadata_indexing.dispatcher`、`search`、`rebuilder` 在用例内部读取全局配置并构造 MySQL、Elasticsearch、Qdrant 与 TEI 客户端。
- `metadata_indexing.value_refresh` 将游标策略、规范值、Data Sync 表读取、频次 SQL、外部发布和状态机编排集中在约 1,500 行文件中。
- `data_sync.backfill` 直接导入 `enqueue_value_refresh`、`prepare_frequency_mutation` 和 `apply_frequency_row_changes`，形成实现级双向依赖。
- 当前测试大量直接驱动 `_synchronize`、helper、SQL 调用顺序和伪 collaborator；需要在新的公共 seam 覆盖等价行为后替换，而不是叠加新套件。

## Decisions

### 1. Ownership and hard package move

`src/data_agent/metadata_indexing/` 硬迁移为 `src/data_agent/ddl_metadata/meta_projection/`。活动代码、测试和规范一次性更新 import；不保留旧包兼容 shim。Meta Snapshot 仍是权威状态，Meta Projection 仅保存可重建的 desired work、频次中间态及 Qdrant/Elasticsearch 派生表示。

目标结构只创建有真实职责的模块：

```text
src/data_agent/ddl_metadata/
├── application/
│   └── accepted_snapshot.py        # 发布命令与 SnapshotPublisher port
├── adapters/mysql/
│   └── accepted_snapshot.py        # generation lock + 单事务集成适配器
└── meta_projection/
    ├── domain.py                    # work/value types 与纯版本、游标、规范化策略
    ├── application/
    │   ├── contracts.py             # work store、authority reader、index ports
    │   ├── dispatcher.py            # claim -> remote -> settle 用例
    │   ├── value_refresh.py         # 有界刷新状态机用例
    │   ├── search.py                # 候选召回 + Meta 权威回读用例
    │   └── rebuild.py               # 重建用例
    └── adapters/
        ├── mysql.py                 # outbox、projection read、频次与发布 SQL
        ├── elasticsearch.py         # value index
        ├── qdrant.py                # semantic index
        └── composition.py           # 配置、客户端和具体实现装配
```

如迁移时单个适配器仍过大，可按 `mysql_work.py`、`mysql_values.py` 拆分，但不得按每个 SQL 方法创建浅模块。`tables.py` 属于 MySQL adapter schema，可与现有集中 metadata 注册方式保持一致。

### 2. Accepted snapshot publication is one deep interface

工作流只依赖以下应用接口，不感知 SQLAlchemy、锁实现或其他 context repository：

```python
@dataclass(frozen=True)
class AcceptedSnapshot:
    schema: PhysicalSchema
    metadata: SemanticMetadata
    questions: tuple[MetricQuestion, ...]
    answers: tuple[MetricAnswer, ...]
    metrics: tuple[MetricMetadata, ...]
    candidates: tuple[MemoryCandidate, ...]

class AcceptedSnapshotPublisher(Protocol):
    async def publish(self, snapshot: AcceptedSnapshot) -> None: ...
```

这些输入类型继续以现有共享 contract 模块为权威来源：`PhysicalSchema` 来自 `data_agent.models.physical`，`SemanticMetadata`、`MetricQuestion`、`MetricAnswer` 与 `MetricMetadata` 来自 `data_agent.models.semantic`，`MemoryCandidate` 来自 `data_agent.models.memory`。本子任务不把 ORM row、外部响应 DTO 或生成类型引入该接口，也不借包迁移重定义这些共享 contract。

`DDLGraphDependencies` 接收该 port；工作流节点只组装命令并调用 `publish`。具体 `MySQLAcceptedSnapshotPublisher` 是 DDL Metadata 外层的集成适配器，可以在同一个 session 中调用 Meta、Memory、Data Sync desired 与 Meta Projection MySQL writers，因为跨 context 的单库原子提交是此项目的明确一致性要求。

发布顺序保留现有不变量：获取相关 generation locks；开启一个 MySQL transaction；锁定并读取 previous scope；写入 accepted Meta；过期/写入 Memory；发布 Data Sync desired；计算并 enqueue Meta Projection desired states；提交；最后释放 locks。任一步失败先完整 rollback，再释放 locks。commit 成功后的 lock release 失败只记录基础设施错误，不反转成功结果。

该适配器是 anti-corruption/integration seam，不承载 desired version、投影内容或搜索规则。不能把其内部 repository 方法逐一复制成宽大的 UnitOfWork protocol。

### 3. Pure policy versus application and adapters

- `domain.py` 保留不执行 I/O 的类型与确定性函数：desired version、semantic desired states、值规范化、游标编码/比较、刷新代次转换和 bulk budget 判断。
- `application` 用例只依赖构造时注入的 ports 和普通配置值；禁止读取全局 settings、构造客户端或导入 SQLAlchemy tables。
- MySQL adapters 负责 claim/lease/ack/backoff/defer、authoritative projection read、value frequency persistence 与 Data Sync relational lookup。
- Qdrant/TEI、Elasticsearch adapters 负责 SDK payload、mapping/setup 和远程调用；outbox 已提供重试语义时，客户端层不叠加重试。
- composition root 在 worker lifecycle/startup 创建 concrete adapters；maintenance cron 只调用已装配的 use case，不再在函数体内构造 dispatcher。

### 4. Meta Projection input for Data Sync

本子任务公开 Data Sync 可消费的中立输入模型，不暴露 `DesiredSyncTable`、`data_sync_task`、SQLAlchemy `AsyncSession` 或 Meta Projection repository：

```python
@dataclass(frozen=True)
class MaterializedTableRef:
    table_id: str
    source_id: str
    source_schema: str
    source_table: str
    target_table: str
    primary_key: tuple[str, ...]

@dataclass(frozen=True)
class MaterializedRowsChanged:
    table: MaterializedTableRef
    before_rows: tuple[Mapping[str, object], ...]
    after_rows: tuple[Mapping[str, object], ...]
    checkpoint: Mapping[str, object]

class PreparedValueProjection(Protocol):
    @property
    def needs_before_rows(self) -> bool: ...

    async def apply(self, change: MaterializedRowsChanged) -> None: ...

class ValueProjectionParticipant(Protocol):
    async def prepare(
        self,
        table: MaterializedTableRef,
    ) -> PreparedValueProjection: ...
```

`ValueProjectionParticipant` 表示“加入调用方当前 MySQL transaction 的值投影参与者”。具体实现由外层调用点绑定到同一 transaction-scoped adapter；应用接口本身不接收 session。调用方必须在 DW DML 前调用 `prepare`，使实现先锁定当前来源的适用状态；DML 完成后再通过返回的 `PreparedValueProjection.apply` 提交 before/after 行变化，在同一事务内完成频次增量和 refresh desired enqueue。`needs_before_rows` 让调用方只在当前状态确有需要时读取加锁的 DW before 镜像。对于无稳定 plan、pending structure generation 或尚未 materialize 的表，保持现有安全跳过语义，后续全量 SCAN 建立基线。

该 participant 不是进程级 singleton。外层 transaction factory 在 Data Sync 每次创建 transaction-scoped repository 时，用同一个 `AsyncSession` 创建 participant，再把两者注入当前调用；session 不进入 application contract。集成测试通过同一故障注入同时观察 DW/Data Sync 写入、频次变化与 refresh enqueue 全部 rollback，证明共享 transaction identity，而不是只断言 mock 调用。

后续 Data Sync 子任务在自己的 application 层定义所需 outbound port，并由外层桥接到该输入接口；Data Sync 不再导入 Meta Projection helper。这样依赖关系是 Data Sync use case -> 自有 port <- integration adapter -> Meta Projection input，而不是两个 context 互相导入实现。

### 5. Projection lifecycle ports

只为独立变化或测试替换有价值的协作者定义端口：

| Port | Deep operation | Production adapter |
|---|---|---|
| `ProjectionWorkStore` | claim bounded work; renew; settle success/defer/backoff/progress while enforcing full authority identity | MySQL outbox adapter |
| `ProjectionReader` | build authoritative semantic/value plans and revalidate candidates against Meta | MySQL projection adapter |
| `SemanticIndex` | setup/reset/upsert/delete/search semantic projection | Qdrant + TEI adapter |
| `ValueIndex` | setup/reset/publish/delete/search/read visible generations | Elasticsearch adapter |
| `ValueRefreshStore` | execute one bounded scan/select/publish/cleanup persistence unit | MySQL value adapter |

Dispatcher 的公共行为是 `dispatch(limit) -> processed_count`，内部对每项执行短 claim transaction、事务外远程调用、短 settle transaction。原始基础设施异常继续用于 retry/defer/dead-letter 分类；取消异常必须传播。

Search 的公共行为保持不变：先从派生索引取有界候选，再从 Meta 权威状态回读；value search 还要比较所有可见 refresh generations，并在并发刷新时正确标记 `complete`。

## Dependency Rules

| From | May depend on | Must not depend on |
|---|---|---|
| DDL Metadata workflow/application | domain values, application ports | persistence modules, SQLAlchemy, Data Sync repository/tables |
| Meta Projection domain | standard library, shared contract base if required | FastAPI, SQLAlchemy, settings, external SDKs, Data Sync |
| Meta Projection application | domain, its ports, injected scalar config | adapters, tables, global client factories, Data Sync implementation |
| Meta Projection adapters | application/domain contracts, infrastructure resources | workflow/node internals or domain policy ownership |
| Data Sync application | its own outbound port and neutral commands | Meta Projection implementation modules |

Static import tests enforce the inner-layer prohibitions. A full AST import-cycle check remains part of verification, but passing it alone does not replace the ownership checks.

## Data and Failure Flow

### Accepted snapshot

```text
workflow node
  -> AcceptedSnapshotPublisher.publish(command)
  -> acquire generation locks
  -> one MySQL transaction
       Meta authoritative write
       Memory lifecycle write
       Data Sync desired write
       Meta Projection desired/outbox write
  -> commit or complete rollback
  -> release locks
```

### Projection dispatch

```text
short MySQL claim
  -> authoritative projection plan
  -> remote Qdrant/TEI or Elasticsearch work outside row lock/transaction
  -> short MySQL settle: acknowledge | progress | defer | backoff | dead-letter
```

### Materialized row change

```text
Data Sync use case -> own ValueProjection port
  -> transaction-scoped integration adapter
  -> Meta Projection ValueProjectionParticipant.prepare(table)
  -> DW DML
  -> PreparedValueProjection.apply(change)
  -> frequency delta + refresh desired in the caller's MySQL transaction
```

## Test Replacement Strategy

测试按 `tdd` 的 red-green-replace 纵向推进：先通过公共 seam 写失败测试，完成最小迁移使其通过，再删除被覆盖的实现耦合测试。

### Accepted Snapshot seam

- 保留并迁移四个事务不变量：lock 覆盖 commit、rollback 先于 unlock、commit 后 unlock 失败不反转成功、lock contention 不开启事务。
- 增加一个 transaction-level 集成测试证明 Meta、Memory、Data Sync desired 与 projection outbox 要么一起提交，要么一起回滚。
- 删除只断言具体 repository 构造/调用序列、但不证明事务结果的测试替身。

### Meta Projection seam

- 通过 dispatcher 公共接口证明 claim -> remote -> settle、取消传播、local progress defer、dead-letter/backoff 和一次有界 value state transition。
- 通过 search 公共接口证明 semantic/value 候选均经过 Meta 权威回读、generation 并发检测和 bounded candidate budget。
- 通过 `ValueProjectionParticipant.apply` 证明 before/after 频次变化、pending generation 安全跳过、同一 transaction enqueue refresh。
- 保留 adapter contract tests：ES mapping/analyzer、Qdrant vector/payload schema、MySQL lease authority、cursor native ordering、bulk byte budget。这些是外部兼容或持久化协议，而非内部实现细节。
- 在新 seam 覆盖后删除直接调用 `_synchronize`、`_scan`、`_select_top_n`、`_publish`、`_cleanup` 以及重复 generation/cursor 场景的测试；相同不变量只保留最低成本的一层。

目标测试目录迁移到 `tests/unit/ddl_metadata/meta_projection/` 与 `tests/integration/ddl_metadata/`；不保留 `tests/unit/metadata_indexing/` 兼容路径。

## Compatibility and Migration

- 不改变 HTTP/SSE payload、MySQL schema/table/column、Redis keys、Qdrant collection、Elasticsearch index/mapping、TEI 协议、配置键、日志事件或 LangGraph node/state 名称。
- 不增加数据库、向量索引、历史数据迁移、双写或清理路径。现有投影保持可重建；重构仅移动代码所有权和依赖方向。
- 内部 Python import 是硬迁移，所有活动调用方与测试同步更新。
- 本子任务为了完成硬包迁移，最小改写 Data Sync 现有外层调用点，使 backfill/reset/buffered-binlog 只调用新的公共 projection input；不得保留任何 `data_agent.metadata_indexing` import，也不得在本任务内设计 Data Sync application port 或重排其用例。后续 Data Sync 子任务负责把这些调用改为 Data Sync 自有 outbound port 和正式组合根桥接。

## Rollout and Rollback

1. 先建立纯策略与公共 seam 测试，再移动实现；每个纵向切片保持测试可运行。
2. 先完成 accepted snapshot，再完成 projection lifecycle/search，最后落地 Data Sync input adapter。
3. 本子任务评审并冻结输入接口后，Data Sync 子任务才可激活。
4. 若单事务、authoritative readback 或 outbox convergence 任一不变量无法证明，回滚本子任务，不引入旧/新路径并存的兼容层。

## Decision Record

跨 context 的 accepted snapshot 使用一个 MySQL 集成适配器是有意的例外：它牺牲完全独立的 context 持久化，以换取项目已要求的 Meta、Memory、Data Sync desired 与 projection outbox 原子可见性。该决定应在实现完成且验证成立后记录 ADR；在规划评审前不提前写入正式 ADR。
