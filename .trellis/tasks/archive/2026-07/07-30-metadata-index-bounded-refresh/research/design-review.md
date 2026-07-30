# 设计评审记录

## 评审范围

- 状态机与 desired version 抢占。
- Elasticsearch DISCOVER/PUBLISH/CLEANUP 的有界性、幂等和崩溃恢复。
- MySQL 主键范围扫描与 CDC 并发。
- 精确频次、Top-N 淘汰、共享 DW target 和重复投递。

## 发现及处置

### 已关闭的阻塞项

1. **旧 ID 会在发布新 ID 后丢失。**
   原草案按 value 保存单个 published ID，无法同时保留旧、新文档。现改为
   `metadata_value_publication` 每个真实 ES document ID 一行；旧 ID 和稳定新 ID
   独立持久化，CLEANUP 可明确删除旧 ID。
2. **跌出 Top-N 的值没有 tombstone。**
   原草案只 upsert desired。现改为 `desired_membership_version`：当前集合只包含
   membership 等于当前 desired version 的行；未入选旧行自然成为 cleanup 候选。
3. **DISCOVER 分页没有一致性边界。**
   用户确认功能尚未投入使用，因此最终设计删除旧索引 DISCOVER/迁移范围，以空索引
   和空 publication 表开始；不再需要为未存在的线上旧文档建立基线。

### 已关闭的重要缺口

- `bulk_cursor` 改为包含 phase/version/generation 的结构化游标，phase 切换清空。
- action journal 保存不可变 payload/hash；未知结果重放，DELETE 404 成功，确定性
  4xx 永久失败，旧 pending UPSERT 在不再 desired 时转换为 DELETE。
- “Top-N 覆盖索引”改为可实现的排序索引；最多定位 N 个主键并回表读取 N 行 TEXT。
- 明确所有 backfill/CDC 路径统一
  `metadata state -> DW row -> frequency row` 锁顺序。
- 明确复合主键 cursor 的版本、schema fingerprint、列顺序、类型和数据库比较语义。
- 负频次不再含糊地视为幂等：先由现有 event/cursor 去重；未应用事件造成负数是
  永久 invariant error，事务回滚。
- 最终 fresh bootstrap 的 VALUES 行直接初始化为 SCAN，不复用 SEMANTIC 的 NULL phase。

## 评审结论

上述阻塞项已在 `design.md` 与 `implement.md` 中形成可执行的数据契约和测试点。
设计可以进入用户评审；用户批准前不启动 Trellis implementation phase。
