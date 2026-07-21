# 初始化记忆投影版本为 v1

## Goal

让尚未实际使用或发布的记忆派生索引从 `projection_version: v1` 开始，
避免在不存在旧投影兼容需求时提前消耗版本号。

## Background

- `conf/app_config.yaml:71` 当前配置为 `projection_version: v2`。
- `.trellis/spec/backend/conversation-memory.md:92` 当前规范示例同样为 `v2`。
- `.trellis/tasks/07-20-mem0-long-term-memory/design.md:304` 和
  `implement.md:51` 仍要求 bump 投影版本并重建索引。
- 用户确认该投影尚未实际使用，因此没有需要隔离或迁移的既有 `v1` 索引。

## Requirements

- 初始 `memory.projection_version` 必须为 `v1`。
- 运行配置与当前 Trellis 规范必须保持一致。
- 同步纠正 `07-20-mem0-long-term-memory` 的设计与执行清单，不再要求对尚未
  使用的初始投影执行版本 bump 或重建。
- 不新增迁移、兼容分支、配置项或测试框架。
- 仅在已经发布的投影发生不兼容结构变更时再递增版本号。

## Acceptance Criteria

- [x] `conf/app_config.yaml` 中 `memory.projection_version` 为 `v1`。
- [x] 当前规范不再要求初始投影使用 `v2`。
- [x] `07-20-mem0-long-term-memory` 的规划与初始 `v1` 决策一致。
- [x] 配置加载检查通过。
- [x] 与首次版本选择无关的运行逻辑保持不变。

## Out of Scope

- 执行索引 recreate 或全量 rebuild。
- 修改内容结构版本 `memory.content_version: v1`。
- 为未来投影版本引入自动迁移机制。
