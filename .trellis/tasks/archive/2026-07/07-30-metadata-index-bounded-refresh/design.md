# 元数据字段值索引有界刷新设计

## 1. 设计结论

采用一个由 `metadata_indexing` 拥有的深 module：

```python
await MetadataValueRefresh.run_next_unit(
    claimed_work,
    budget,
) -> MetadataValueWorkResult
```

dispatcher 只负责 claim、调用和错误分类。`SCAN / SELECT_TOP_N /
PUBLISH / CLEANUP / COMPLETE`、游标、精确频次、新旧集合差集和恢复协议均隐藏在 module
implementation 内。data-sync 只通过一个事务内频次 mutation interface 写 MySQL，
不调用 Elasticsearch。

该设计显式 supersede 两条旧决策：

- 不再对完整 DW 字段执行 `GROUP BY / ORDER BY / LIMIT`。
- 不再用整表 `delete_by_query` 清理旧版本。

不采用单纯增加 timeout、`GROUP BY LIMIT/OFFSET`、近似频次算法或 Elasticsearch
近似 terms。

## 2. Module 与 seam

```text
data_sync backfill / CDC transaction
  -> MetadataValueFrequencyRepository.apply_row_changes(...)
  -> MetadataIndexOutboxRepository.enqueue(...)

metadata worker cron
  -> MetadataIndexDispatcher
  -> MetadataValueRefresh.run_next_unit(...)
       -> DynamicDWScanner adapter (MySQL Core)
       -> MetadataValueFrequencyRepository
       -> MetadataValuePublicationRepository
       -> MetadataValueElasticsearchIndex adapter

internal search
  -> existing metadata value search + completeness check
```

外部 interface 保持窄：

```python
class MetadataValueWorkStatus(StrEnum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    DEFERRED = "deferred"

class MetadataValueWorkResult(ContractModel):
    status: MetadataValueWorkStatus
    phase: MetadataValueRefreshPhase

class MetadataValueRefresh:
    async def run_next_unit(
        self,
        item: ClaimedMetadataIndexWork,
        budget: MetadataValueWorkBudget,
    ) -> MetadataValueWorkResult: ...
```

`Phase`、主键 cursor 和 bulk cursor 是内部持久化契约，不由 dispatcher 计算。
MySQL 与 Elasticsearch adapters 是内部 seams，测试可替换；不把 adapters 放进
公开调用参数。

## 3. 持久化模型

### 3.1 `metadata_index_outbox` 扩展

VALUES/TABLE 行从“完成即删除”改为持久化状态；SEMANTIC 行沿用完成删除。

新增字段：

| 字段 | 含义 |
|---|---|
| `phase` | `SCAN / SELECT_TOP_N / PUBLISH / CLEANUP / COMPLETE` |
| `frequency_version` | 当前精确频次 generation，只随物理 schema、资格或规范化规则变化 |
| `pending_frequency_version` | 活跃 work unit 后待切换的频次 generation |
| `last_primary_key` | 当前 SCAN 字段最后提交的带 schema 版本的类型化复合主键 JSON |
| `bulk_cursor` | 带 `phase + desired_version + index_generation + last_document_id` 的 PUBLISH/CLEANUP 游标 |
| `index_generation` | 当前 Elasticsearch index 实例 generation |

复用：

- `progress_column_id`：当前阶段正在处理或最后完成的字段。
- `desired_version` / `pending_desired_version`：当前与待处理发布版本。
- `lease_token` / `lease_expires_at`：work unit authority。
- `attempts` / `available_at` / `last_error_type`：远程失败预算。

VALUES 行初始化为 `SCAN`，不存在 `phase IS NULL` 的 VALUES 兼容语义。原
SEMANTIC 行仍以 `phase IS NULL` 表示旧生命周期。claim 选择原 SEMANTIC 行或 VALUES 中
`phase != COMPLETE` 的行。完整性查询只把 VALUES 非 COMPLETE、dead-letter 和全局
REBUILD 行视为 pending。每次 phase/version/generation 变化都清空不属于新阶段的
cursor；解析 `bulk_cursor` 时必须校验其内嵌身份，禁止跨阶段复用。

### 3.2 `metadata_value_frequency`

精确频次表：

```text
table_id           VARCHAR(128)
column_id          VARCHAR(128)
frequency_version  CHAR(64)
value_hash         CHAR(64)
value_text         TEXT
frequency          BIGINT UNSIGNED
updated_at         DATETIME
```

约束与索引：

- 主键：
  `(table_id, column_id, frequency_version, value_hash)`。
- 精确 Top-N 排序索引：
  `(table_id, frequency_version, column_id, frequency DESC, value_hash)`。
- `value_hash = SHA256(canonical value_text)`；同一 hash 出现不同
  `value_text` 时按 invariant violation 回滚，不静默合并。
- `frequency > 0` 才保留；delta 后为 0 时删除。事件先由现有唯一 coordinate/cursor
  判定是否已应用；已应用事件直接跳过。未应用事件产生负数属于不可恢复 invariant
  violation，整笔 DW/频次/event-ack 事务回滚并记录永久错误，不把负数当作幂等成功。
- 旧 `frequency_version` 不参与当前读取，后续按主键 keyset 有界清理。

`frequency_version` 只包含：

- 物理 target table 及 peer schema fingerprints；
- logical `table_id / column_id / physical column name / data type` 映射；
- 字段 eligibility；
- canonical value normalization version。

CDC 位点变化不会创建新 frequency generation。

### 3.3 `metadata_value_publication`

该表按“一个真实 Elasticsearch 文档 ID 一行”保存期望成员、已发布集合和远程
action journal，使每个待发布或待删除 ID 都有独立、可恢复的状态：

```text
table_id
index_generation
document_id
column_id
value_hash
value_text
desired_membership_version   -- NULL 表示不属于当前物化版本
desired_frequency
desired_payload_hash
published_payload_hash       -- NULL 表示数据库不认为远端存在
pending_action        -- upsert / delete / NULL
action_version
action_payload_hash
action_payload_json           -- UPSERT 的完整不可变请求体；DELETE 为 NULL
updated_at
```

约束与索引：

- 主键：`(table_id, index_generation, document_id)`。
- 新 desired 行的 `document_id =
  SHA256(table_id + NUL + column_id + NUL + value_hash)`。
- desired 查询索引：
  `(table_id, index_generation, desired_membership_version, column_id, document_id)`。
- published 查询索引：
  `(table_id, index_generation, published_payload_hash, document_id)`。
- action 恢复索引：
  `(table_id, index_generation, action_version, pending_action, document_id)`。

`SELECT_TOP_N` 为本字段查询出的每个稳定 ID 写
`desired_membership_version = current desired_version`。没有入选的新旧行不会被改写；
因此其 membership version 与当前版本不同，是可执行的 tombstone。只有所有字段均
成功物化后才能进入 PUBLISH，故“当前 desired 集合”严格定义为
`desired_membership_version = state.desired_version`。publication 表本身就是上一版
已发布 ID 集合的权威记录。

远程调用前先把 action 类型、document ID、目标 payload hash/body 与 action version
持久化；远程成功后用完整 `desired_version + lease_token` CAS 更新
`published_payload_hash`、清除 action 并推进 `bulk_cursor`。请求成功但数据库提交
失败时 action 仍 pending，下一次以相同稳定 ID 和 payload 幂等重放。DELETE 404
视为成功；单项 429/5xx 和 transport unknown 可按有限 retry budget 重放；认证、
mapping、超大文档和其他确定性 4xx 进入永久错误。版本提升时旧 pending UPSERT
不能直接丢弃：若它不再 desired，则转换为同一 ID 的 DELETE action；DELETE 重放
完成前不得 COMPLETE。这样即使旧 UPSERT 的结果未知，也由最后一次显式 DELETE
收敛。

## 4. 状态机

```text
SCAN
  -> 锁状态行
  -> 按 progress_column_id + last_primary_key 读取一批 DW 原始行
  -> 累加 frequency generation
  -> 同事务推进主键 cursor
  -> 当前字段结束后切下一个字段；全部结束 -> SELECT_TOP_N

SELECT_TOP_N
  -> 每次处理一个字段
  -> 使用排序索引读取精确 Top-N
  -> 将入选行标为当前 desired_membership_version
  -> 全字段完成 -> PUBLISH

PUBLISH
  -> 准备一批 missing/changed UPSERT action
  -> 事务外有界 ES bulk
  -> CAS 结算 published 集合与 bulk_cursor
  -> 无 UPSERT -> CLEANUP

CLEANUP
  -> 准备一批 membership 不是当前版本、旧 document ID 或未知旧 action 的 DELETE
  -> 事务外按显式 `_id` 有界 bulk delete
  -> CAS 清理 published/action 状态与 bulk_cursor
  -> 有界清理无效 publication/frequency generation 行
  -> ES refresh 成功 -> COMPLETE

COMPLETE
  -> 保留状态行作为 CDC 频次维护与下一 desired version 的稳定锚点
  -> search completeness 可为 true
```

每次 claim 最多执行一个行 batch、一个字段 Top-N 或一个 ES bulk，不在一个 claim
中循环到 phase 完成。PUBLISH/CLEANUP 使用带阶段身份的 `bulk_cursor`。进入下一阶段
前事务性清空前一阶段 cursor。

## 5. SCAN 与并发 CDC

### 5.1 稳定主键扫描

- 复用 `DesiredSyncTable.primary_key` 和现有类型化 cursor 编解码。持久化 envelope
  固定为 `v=1 + schema_fingerprint + ordered columns + typed values`；恢复时字段名、
  顺序、类型和 fingerprint 必须全部匹配。主键列按 MySQL 约束不可为 NULL，实际
  比较使用解码后的原生绑定值和数据库 collation，不比较 JSON 文本。
- 动态 DW 表通过 SQLAlchemy Core `table()` / `column()` 构造。
- 使用复合 keyset：
  `WHERE (pk...) > (:cursor...) ORDER BY pk... LIMIT :scan_batch_size`。
- SCAN 事务先锁对应刷新状态行，再对有限 DW 行使用 `FOR UPDATE`，累加频次和推进
  cursor 后一次提交。
- 无主键或不可靠排序主键继续由现有 data-sync 接受门禁拒绝。

### 5.2 CDC/backfill 频次 mutation

data-sync 在原 DW 事务内调用：

```python
await frequency_repository.apply_row_changes(
    desired,
    before_rows,
    after_rows,
)
```

以下顺序是所有 data-sync backfill、buffered CDC INSERT/UPDATE/DELETE 路径的强制
契约，而不是仅对新 repository 的建议：

1. 根据共享 target peer 与 eligibility 解析 physical name 到 logical
   `(table_id, column_id)` 映射。
2. 按 `table_id` 排序锁对应持久化状态行。
3. 执行 DW DML。
4. 按 `(table_id, column_id, frequency_version, value_hash)` 排序锁定/更新频次行，
   在同一事务中合并规范化 delta。
5. 确认 backfill cursor 或 CDC event/coordinate，并 enqueue 最新 desired state。

操作语义：

- INSERT：新值 `+1`。
- DELETE：旧值 `-1`。
- UPDATE：旧值 `-1`、新值 `+1`；相同规范值净变化为 0。
- 主键变化：按旧主键 DELETE 和新主键 INSERT 分别判断。

SCAN 中的条件更新：

- 已扫描完成的字段：应用 delta。
- 当前字段且事件 PK `<= last_primary_key`：应用 delta。
- 当前字段游标之后或尚未扫描字段：不应用；未来 SCAN 读取 DW 当前行。
- 尚未建立 generation：不应用；后续 SCAN 建立基线。
- SELECT_TOP_N/PUBLISH/CLEANUP/COMPLETE：对当前 generation 应用 delta，并产生新的
  desired version；当前已物化发布可继续完成一个有界单元，随后切到最新版本。

全路径统一使用 `metadata state -> DW row -> frequency row` 锁顺序。现有先做 DW
DML 的路径必须重排后才能接入频次维护，禁止保留旁路。这样扫描与 INSERT 插入游标
之前的新行不会形成“扫描已越过但 CDC 也跳过”的窗口。真实 MySQL 测试覆盖当前事务
隔离级别下 `FOR UPDATE`/next-key lock 的游标边界插入；锁等待必须受数据库既有超时
约束并作为可重试事务失败，而不是扩大 worker 单元。

### 5.3 重复投递

- backfill 的 DW 写、频次 delta 与 `last_backfill_key` 在同一事务；
  回滚后整体重放，提交后 cursor 阻止重复。
- CDC 的 DW DML、频次 delta、`acknowledged_at` 和 applied coordinate 在同一事务；
  `(source, binlog_file, position, row_index)` 唯一键阻止重复捕获。
- 不新增第二套事件去重真相源；集成测试从 capture/service 边界证明同一事件重复投递
  不改变频次。

## 6. 精确 Top-N 与发布

- 排序固定为 `frequency DESC, value_hash ASC`。
- 每字段最多 `value_top_n`，当前为 10,000。
- 查询通过上述排序索引定位前 N 个主键，再对最多 N 行回表读取 `value_text`；
  `TEXT` 不宣称被二级索引覆盖。扫描索引项和回表行数均受 N 限制，不执行全表聚合。
- SELECT_TOP_N 在锁定状态行的单个短事务中物化一个字段，CDC 在该事务后更新频次并
  enqueue 新 desired version；因此每个 desired 集合对应一个确定时点。
- PUBLISH 比较 desired 与 published 的文档 ID、频次和 payload；新增及频次变化为
  UPSERT。
- CLEANUP 比较 published、旧 pending action 与当前 desired；只生成明确 ID 的
  DELETE。
- ES bulk 继续使用文档数和 NDJSON 字节双预算；单文档超限明确失败，不自动截断值。
- 禁止 `delete_by_query`。

## 7. 新版本、租约与迟到 worker

- 新 desired 到达活跃 work unit 时先写 `pending_desired_version`，不并发启动第二个
  lease；当前单元已被预算保证有界。
- 当前单元结算时：
  - frequency version 相同且仍在 SCAN：提升 desired version但保留扫描 cursor，
    避免持续 CDC 让基线永远重启。
  - frequency version 相同且在 SELECT_TOP_N/PUBLISH/CLEANUP：提升版本并重置到
    SELECT_TOP_N。
  - frequency version 不同：提升版本，创建新 generation 并重置到 SCAN。
- worker 崩溃时等待 lease 到期后重领；没有两个版本并发 cleanup。
- 所有数据库进度结算匹配 target、kind、object、operation、desired version 与
  lease token。
- 外部调用前后续租；续租失败不推进 cursor。已发生但未结算的 ES action 由
  publication journal 重放或在新版本 cleanup。
- 远程服务失败消耗 retry budget；lease loss、版本提升和数据库结算失败不消耗远程
  budget。

## 8. 共享 DW 目标

- 继续使用 `data_sync_key_owner` 保证每个物理主键只属于一个 source。
- eligibility 仍要求共享同名字段的所有 peer 都通过门禁。
- frequency mutation 按 target table 找到全部 peer，再把一个 physical row change
  投影到相应 logical `table_id / column_id`；不同 logical ID 不共享频次主键。
- 多状态行按 `table_id` UTF-8 字节序锁定，避免不同 source 事件形成锁顺序反转。
- 一个 logical table 的 SCAN 可独立恢复；相同 physical target 的重复扫描只增加
  构建成本，不改变正确性。当前任务不改变既有 outbox 的 logical table 完整性身份。

## 9. Elasticsearch index generation

- 当前功能尚未投入使用，首次启动以空索引和空 publication 表为前提，不实现旧文档
  发现、旧 ID 迁移或在线接管。
- 全量 rebuild recreate index 后生成新的 `index_generation`。旧 generation 的
  publication rows逻辑上视为未发布，不需要对空新索引执行删除；MySQL 旧 generation
  记录后续有界清理。
- search 在 VALUES 非 COMPLETE 或全局 REBUILD 存在时保持 `complete=false`；
  COMPLETE 前不得把部分结果作为完整值域。

## 10. SQLAlchemy Core 决策

项目规范调整为：

- 项目拥有且 schema 固定的控制表使用静态 SQLAlchemy Core `Table`。
- 动态 source/DW 表只允许从已通过 `DesiredSyncTable` 验证的 schema/name/column
  构造 SQLAlchemy Core `table()` / `column()`；业务值和 cursor 始终绑定。
- `text()` 只用于 SQLAlchemy Core 无直接表达的 MySQL 控制语句或 interval 单位，
  动态标识符必须经过单一受审 helper 的 dialect quote，不能来自未验证请求或模型
  输出。

新 SCAN 不使用 `text(f"...")`。旧 `value_projection_batch()` 删除或改为新的
Core keyset scanner；同步更新
`.trellis/spec/backend/database-guidelines.md` 和 `code_review.md` 的既定事实，
避免实现与审查规则继续冲突。

## 11. Schema、兼容与发布

影响范围：

- `metadata_indexing/models.py`：phase、budget、cursor/result contracts。
- `metadata_indexing/tables.py`：状态扩展、frequency、publication 表。
- `metadata_indexing/repository.py`：claim、phase CAS、pending promotion、
  publication/frequency repositories。
- `metadata_indexing/projections.py`：计划解析、规范值和动态 Core scanner。
- `metadata_indexing/elasticsearch.py`：稳定 ID、有界 UPSERT/DELETE bulk；
  删除 `finalize_table(delete_by_query)`。
- `metadata_indexing/dispatcher.py`：委托 `run_next_unit()`。
- `metadata_indexing/rebuilder.py`：index generation 与 publication reset。
- `data_sync/backfill.py`：backfill/CDC 事务内频次 mutation 与锁顺序。
- `desired.py`：区分 frequency version 与 publication desired version。
- bootstrap、配置、fakes、unit/integration tests、active spec。

部署与回滚：

- fresh bootstrap 直接创建新 schema；不提供未使用开发版本之间的线上迁移。
- 新表均为派生状态，不含业务权威；需要重置时通过受控 rebuild 从 DW 重建。
- Meta 与 DW 不从派生表重建，也不因索引失败回滚。

## 12. 验证矩阵

### 单元/仓储

- schema/Core/bootstrap parity。
- phase claim 与完整 authority CAS。
- SCAN 简单/复合 PK cursor、batch rollback、游标边界 INSERT race。
- frequency INSERT/DELETE/UPDATE、归零、hash collision、共享 target。
- 重复 backfill/CDC 事件不重复计数。
- exact Top-N 与 tie-break。
- action prepare、远程成功后 DB 失败、lease loss、新版本提升。
- PUBLISH/CLEANUP cursor 隔离、Top-N 淘汰、新/旧 pending action。
- 禁止 `delete_by_query` 和 `GROUP BY LIMIT/OFFSET` 的结构回归。

### 真实 MySQL + Elasticsearch

- 多批 SCAN 与 worker restart。
- CDC insert/update/delete 和重复事件。
- PUBLISH 第 N 个 bulk 中断并恢复。
- CLEANUP 第 N 个 bulk 中断并恢复。
- 总工作量超过单次任务预算但跨 claim 收敛。
- SCAN/PUBLISH/CLEANUP 各阶段的新 desired version。
- 共享 DW target。
- rebuild index generation。
- 最终 frequency、Top-N、published registry 和 ES 文档完全一致。

### 全量门禁

执行仓库 CI 基线、相关 focused tests、Docker Compose 配置和 `git diff --check`；
真实服务不可用时如实报告，不得声明通过。
