# 修复记忆检索与投影的遗留正确性缺陷

## Goal

修复架构审查记录的三个记忆子系统正确性缺陷：更新响应返回错误的事件编号、
投影版本升级导致检索全量黑障、以及索引重建与调度并发时静默丢失严格映射。

## Background

- P1-19a：`memory/application/service.py` 的 `_replace_memory` 用
  `history(offset=0, limit=100)` 回读后取 `items[-1].id` 作为响应 `event_id`。
  历史按事件 id **升序**分页，因此同一逻辑事实累计事件超过 100 条后，
  `items[-1]` 是第 100 条**旧**事件而不是本次刚写入的事件，`event_id` 从此永远错误。
  DDL 语义记忆经多次重生成很容易越过 100 条。
- P1-19b：`memory/application/search.py` 的最终过滤要求权威行
  `projection_version == 配置值`，否则整条丢弃——**包括 MySQL 精确基线命中**。
  升级 `memory.projection_version` 后、重建任务给行打上新版本号之前，所有记忆
  在检索中消失。派生索引是否收敛已由 `pending_targets` 机制逐信号剔除，因此这条
  行级过滤对 MySQL 权威回查结果是纯粹误伤。
- P1-19c：`memory/indexing/elasticsearch.py` 的 `recreate` 是 delete 后 `setup`。
  两步之间若 dispatcher 恰好执行 upsert，Elasticsearch 默认 `auto_create_index`
  会用动态映射自动建索引；随后 `setup` 的 `indices.exists` 返回 True 直接 return，
  于是 `dynamic: strict` 与 `memory_zh` 分析器全部丢失，中文 BM25 检索质量静默
  劣化且没有任何报错。

## Requirements

- 更新响应的 `event_id` 必须是本次实际写入的事件编号，且不受历史事件总量影响。
- 检索的投影版本约束只用于剔除派生索引信号，不得否决 MySQL 权威回查结果；
  内容版本、租户、状态、内容哈希、有效期、对象白名单等既有校验保持不变。
- `setup` 必须校验既有索引确实具备当前严格映射与分析器；不具备时必须报错，
  不得静默复用被动态映射污染的索引。
- 不改变 `upsert_candidates` 的既有返回契约（快照写入路径同样依赖它）。

## Constraints

- 不引入新的外部依赖，不新增数据库列。
- 不改变公开 HTTP 契约的字段语义。

## Acceptance Criteria

- [x] 逻辑事实的历史事件超过 100 条时，更新响应仍返回本次写入的事件编号。
- [x] 权威行投影版本落后时，MySQL 精确基线命中仍能返回；派生索引信号仍按
      `pending_targets` 剔除。
- [x] 既有索引缺少严格映射或 `memory_zh` 分析器时 `setup` 报错而非静默复用。
- [x] 三个缺陷各有单元测试覆盖。
- [x] README 记录的基础质量门禁全部通过。
