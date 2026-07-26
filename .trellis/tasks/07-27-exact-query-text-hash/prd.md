# 为精确基线检索建立文本哈希索引

## Goal

让精确基线检索走等值索引，消除按 `source` 范围扫描带来的检索延迟。

## Background

`find_exact_query` 对 `agent_memory.memory_text`（TEXT 列）做全等比较。TEXT 列的等值
比较无法走索引，该查询实际只能用到 `source` 前缀，随数据量增长会成为检索延迟主项。

上一轮清理（`07-26-memory-search-hygiene`）把这项记为"待定"，理由是正确修法需要改
数据库结构而项目没有升级迁移机制。用户确认项目尚未部署、不需要考虑迁移，因此本任务
按正确方案实施。

## Requirements

- 为检索文本增加定长哈希列并建立索引，`find_exact_query` 改用哈希等值比较。
- 哈希只用于加速等值查找，不承担内容身份与去重职责——那仍属于 `content_hash`。
- `memory_key` 分支保持不变，两侧都必须是可走索引的等值比较。
- SQLAlchemy Core 定义与 `docs/docker/mysql/data_agent.sql` bootstrap DDL 必须同步。

## Constraints

- 不引入新的外部依赖。
- 不改变检索结果集合与排序行为，只改变查找路径。

## Acceptance Criteria

- [x] `agent_memory` 新增 `memory_text_hash` 列与 `idx_agent_memory_text_hash` 索引，
      Core 定义与 bootstrap 脚本一致。
- [x] 写入路径为每条权威记忆填充该哈希。
- [x] `find_exact_query` 用哈希等值替代 TEXT 全等比较，渲染 SQL 中不再出现
      `memory_text = ?`。
- [x] 有单元测试对比 Core 列集合与 bootstrap 脚本列集合，能在任一处漏改时失败。
- [x] README 记录的基础质量门禁全部通过。
