# 基于 Mem0 的记忆架构实施计划

## 1. 参考与契约

- [x] 固定并记录 `mem0ai/mem0@ddaa655edf41e3ed375b263fb227da0bcd42ccb9d` 的参考文件。
- [x] 删除当前活跃设计/文档中把 `usememos/memos` 作为目标记忆来源的表述；归档历史保留原始记录并在新设计中说明纠正。
- [x] 更新共享 Pydantic 记忆、搜索、历史、事件、链接和 outbox 契约。
- [x] 定义确定性 `memory_text`、content hash、稳定 UID 和 RRF 合并函数。

## 2. MySQL 权威模型硬切换

- [x] 将 SQLAlchemy Core 表替换为 `agent_memory`、`agent_memory_event`、`agent_memory_link`、`memory_index_outbox`。
- [x] 更新 `docs/docker/mysql/data_agent.sql`，仅重建应用记忆表，不触碰 Meta 四表。
- [x] 重写 MemoryRepository 的 ADD、批量读取、历史、链接、软删除、outbox claim/ack/retry 和重建扫描。
- [x] 保持 repository 不提交/关闭 Session。
- [x] 让 MetadataSnapshotService 在同一 Session 提交 Meta、权威记忆、事件、链接和双目标 outbox。
- [x] 添加强制 memory/outbox 失败回滚 Meta 的真实 MySQL 测试。

## 3. 索引投影

- [x] 扩展 Elasticsearch/Qdrant/TEI 设置并保持 `extra=forbid`。
- [x] 实现 ES memory index setup、幂等 upsert/delete、过滤 BM25 search 和专用重建。
- [x] 实现 Qdrant memory collection setup、payload indexes、幂等 upsert/delete、过滤 vector search 和专用重建。
- [x] 使用 TEI document/query 两条 embedding 路径并校验维度、归一化及距离度量。
- [x] 实现共享投影 DTO，禁止 ES/Qdrant 消费者各自解析 MySQL JSON。

## 4. Outbox 与 worker

- [x] 实现有界并发安全 claim、按 target 独立处理、成功确认和失败退避。
- [x] 在 worker startup/shutdown 组合 ES、Qdrant、TEI 生命周期。
- [x] 增加周期性 `dispatch_memory_index_outbox`。
- [x] 增加按 MySQL 游标生成 outbox 的全量重建服务。
- [x] 验证外部写成功但 ack 前崩溃时重放安全。
- [x] 验证索引故障不改变已成功 DDL 任务状态。

## 5. 混合召回

- [x] 先执行 MySQL exact fingerprint 基线检索。
- [x] 并发执行 ES BM25 与 Qdrant vector top-k。
- [x] 通过 RRF、确定性对象命中和稳定 UID tie-break 合并。
- [x] 批量回查 MySQL，过滤软删除、过期版本、hash 不匹配和未同步投影。
- [x] 执行当前 Pydantic、AST/reference 与指标确定性校验。
- [x] 任一索引失败时安全降级；两个索引失败时回退 MySQL exact。
- [x] 对模型只输出有界、类型化、无冲突 capsule。

## 6. 工作流与 API

- [x] 调整 graph state 中的长期记忆候选和复用结果契约，同时保持 checkpoint 仅含可序列化值。
- [x] 保持 `job_id` 短期作用域和 `source` 长期作用域。
- [x] 替换旧 list/pin/archive/correction API 为 search/get/history/update/delete。
- [x] 不提供任意 add API。
- [x] PATCH 保留 kind/scope 并返回 `requires_reprocess=true`。
- [x] DELETE 使用 source lease、软删除、历史和双目标 delete outbox。
- [x] 更新安全错误码、HTTP 映射和有界响应。

## 7. 生命周期、配置和文档

- [x] 更新 FastAPI lifespan 与 worker startup/shutdown 的资源顺序。
- [x] 更新 `conf/app_config.yaml`、设置加载检查和 Docker 配置校验。
- [x] 更新 README 和 `docs/architecture.html` 中的真实 As-Is/To-Be 记忆说明及 SVG。
- [x] 更新当前任务资料，明确 `mem0ai/mem0` 与 LangGraph 的分工。

## 8. 验证

- [x] `uv lock --check`
- [x] `uv run ruff check src tests`
- [x] `uv run pyright src tests`
- [x] `uv run python -m compileall -q src tests`
- [x] `uv run python -m data_agent.settings`
- [ ] `uv run pytest -m "not integration"`
- [x] `docker compose -f docs/docker/docker-compose.yml config`
- [x] 运行真实 MySQL/Redis 持久化、worker 和 DDL flow 测试。
- [ ] 在服务可用时运行 TEI、Elasticsearch、Qdrant 和混合召回集成测试；不可用时明确报告。
- [x] `git diff --check`

## 9. Review Gate

- [x] 使用 `trellis-check` 全量检查 PRD、设计、实现和跨层数据流。
- [x] 按 `code_review.md` 验证每个严重发现后再修复。
- [x] 通过 `trellis-update-spec` 更新数据库、外部服务、API 和质量规范。
- [x] 用户确认后再按项目 Git/PR 规则提交。
