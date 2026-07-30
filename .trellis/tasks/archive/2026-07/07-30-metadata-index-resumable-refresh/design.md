# 元数据字段值索引可恢复刷新设计

## 1. 决策

保留现有表级 `metadata_index_outbox` desired state，不拆字段级 outbox，也不引入新的通用调度框架。在 outbox 行上增加 nullable `progress_column_id`，把一次表级刷新分为：

1. 每次领取只处理排序后的一个 eligible 字段；
2. Elasticsearch 幂等写入该字段当前 `desired_version` 的 Top-N 文档；
3. 用现有完整 authority CAS 持久化 `progress_column_id` 并释放租约；
4. 当游标已经越过最后一个字段时，独立执行表级旧 `refresh_version` 清理；
5. 清理成功后才确认并删除 outbox。

这复用现有 `desired_version + lease_token` interface，把恢复复杂度留在 `metadata_indexing` module 内部；发布方、搜索方和重建调用方无需了解游标。

## 2. 不采用的方案

- 不只延长 600 秒 timeout：表宽和数据量仍可增长，进程退出仍会丢失进度。
- 不拆成字段 outbox + 表 finalize outbox：会扩大 desired-state identity、完整性查询、重建和死信聚合的 interface，并引入跨行依赖竞态。
- 不实现逐行频次计数器：当前字段 Top-N 来自 DW 当前快照；增量计数难以正确处理删除、更新和共享目标。

## 3. 数据契约

`data_sync.metadata_index_outbox` 新增：

```text
progress_column_id VARCHAR(128) NULL
```

含义仅适用于 `target=values, operation=refresh`：最后一个已经完整写入 Elasticsearch 的字段 ID。`NULL` 表示尚未完成字段；游标指向最后一个字段时，下一次执行进入 finalize。

字段顺序固定为 `column_info.id` 升序。若游标在当前计划中不存在，保守地从首字段重做；文档 ID 和 refresh version 使重做幂等。

新 `desired_version` 覆盖旧版本时，enqueue 在同一 UPSERT 中清空游标、租约、失败状态。相同版本的 debounce 合并保留既有游标。

现有环境需要审批后执行精确升级：

```sql
ALTER TABLE data_sync.metadata_index_outbox
    ADD COLUMN progress_column_id VARCHAR(128) NULL
        COMMENT 'values 刷新最后完成的字段标识'
        AFTER desired_version;
```

仓库仍只维护 fresh bootstrap，不增加迁移框架或运行时自动改表。

## 4. 执行状态机

```text
claim(progress=NULL)
  -> upsert first column
  -> CAS progress=first + release lease

claim(progress=first)
  -> upsert next column
  -> CAS progress=next + release lease

claim(progress=last)
  -> delete table documents whose refresh_version != desired_version
  -> acknowledge outbox
```

空计划直接进入 finalize，以便清除已经失去资格的字段文档。

## 5. 并发与故障语义

- 每次外部写前后继续续租；行锁不跨 Elasticsearch 或 DW 查询。
- `advance_progress` 匹配 target、kind、object、operation、desired version 和 lease token；stale worker 更新零行。
- Elasticsearch bulk 成功、游标提交失败时，下一次重复写同一字段；稳定 document ID 使其幂等。
- 取消发生在游标提交前时重做当前字段；发生在提交后时下一次处理后继字段。
- finalize 失败时保留 outbox 和最后字段游标，按现有远程失败预算退避；重试只重做清理。
- 新 desired version 到达会清空游标并使旧 lease 失效；旧版本文档只由新版本最终 finalize 清理。
- outbox 存在期间搜索完整性保持 `complete=false`，无需修改搜索 interface。

## 6. Module interfaces

`MetadataIndexOutboxRepository` 增加一个内部 interface：

```python
advance_progress(item, column_id) -> bool
```

它原子保存游标、释放租约并立即允许下一次领取。

`MetadataValueElasticsearchIndex` 把原先一次完成的 `refresh_table` 拆为两个窄 interface：

```python
upsert_projections(projections, heartbeat=None) -> None
finalize_table(table_id, refresh_version, heartbeat=None) -> None
```

调用顺序由 dispatcher 隐藏，其他模块不直接使用。

## 7. 验证与回滚

- 单测覆盖新版本清游标、claim 携带游标、游标 CAS、逐字段推进、取消后从游标继续、最终清理。
- 现有 outbox authority、dead-letter、搜索完整性和 bootstrap parity 测试必须继续通过。
- 真实 MySQL + Elasticsearch 集成验证跨多次领取后的最终文档集合；Docker 不可用时明确记录缺口。
- 回滚代码前应停止 dispatcher。数据库新增 nullable 列可保留；旧代码忽略该列。索引是可重建投影，必要时从权威 Meta/DW 重建。
