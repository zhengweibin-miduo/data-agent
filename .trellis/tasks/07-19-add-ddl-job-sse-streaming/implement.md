# DDL 任务 SSE 流式输出实施计划

## 1. 公共契约与配置

- [ ] 在 `models/jobs.py` 增加稳定阶段和事件数据模型，并为 `DDLJobAccepted` 增加 `events_url`。
- [ ] 在 `settings.py` 与 `conf/app_config.yaml` 增加严格校验的心跳间隔和事件条数上限。
- [ ] 增加模型与配置单元测试，确认不包含内部 DDL、提示或异常文本。

## 2. Redis 事件边界

- [ ] 在 `jobs/redis/keys.py` 增加任务事件 Stream 键。
- [ ] 新增 `jobs/redis/event_store.py`，实现有界 `XADD`、TTL、尾 ID 和有界阻塞 `XREAD`。
- [ ] 通过 `DDLJobStore` 暴露发布、读取和快照所需的应用级方法，API 与 Worker 不直接导入 Redis 实现。
- [ ] 状态转换、提交和回答受理后发布对应事件；事件失败记录安全告警且不改变业务状态。
- [ ] 增加 Redis Store 单元测试和真实 Redis 集成覆盖。

## 3. Worker 与 LangGraph 进度

- [ ] 将 `graph.ainvoke` 改为完全消费 `graph.astream(..., stream_mode="tasks", version="v2")`。
- [ ] 只处理 task-start 的节点名，通过集中映射生成稳定业务阶段。
- [ ] 保持 `durability="sync"`、interrupt、恢复、重试、终态投影和 checkpoint 清理语义。
- [ ] 增加测试覆盖节点映射、修复重试、等待回答和终态路径。

## 4. SSE API

- [ ] 新增 `/api/v1/metadata/ddl-jobs/{job_id}/events` 路由和独立 SSE 编码/生成模块。
- [ ] 新连接发送当前快照；终态快照后关闭。
- [ ] 从当前 Stream 尾 ID 后读取新事件，阻塞超时后发送心跳并重读权威状态。
- [ ] 检测客户端断开并释放生成器；流启动后的 Redis 错误发送安全 `stream_error` 后关闭。
- [ ] 增加 404、503、响应头、帧格式、快照、进度、等待回答、终态关闭、事件缺失修复和断线清理测试。

## 5. 兼容与质量验证

- [ ] 运行 `uv lock --check`。
- [ ] 运行 `uv run ruff check src tests`。
- [ ] 运行 `uv run pyright src tests`。
- [ ] 运行 `uv run python -m compileall -q src tests`。
- [ ] 运行 `uv run python -m data_agent.settings`。
- [ ] 运行相关单元测试和 `uv run pytest -m "not integration" -q`。
- [ ] Redis 可用时运行相关集成测试；不可用时明确报告未验证项。
- [ ] 运行 `git diff --check`，检查现有 API、Redis 状态协议、日志字段和未相关工作树改动。
- [ ] 按 `code_review.md` 执行最终审查。

## 风险与回滚点

- LangGraph `tasks` 事件形状：先用聚焦测试锁定 v2 task-start 判别，失败则回滚到显式节点进度回调。
- SSE 测试可能因无限响应而挂起：测试终态快照或直接测试有界异步生成器，不让 ASGITransport 等待无限流结束。
- 状态与事件两步写入：依靠每次心跳重读权威 Hash 修复，不把事件变成状态源。
- 事件写入不得让已成功业务状态回滚；故障只记录并由快照修复。
- 回滚只删除新增通知路径并恢复 `ainvoke`，不迁移或删除既有任务数据。
