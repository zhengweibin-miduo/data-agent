# 为 DDL 元数据任务增加 SSE 流式输出

## Goal

为已经异步受理的 DDL 元数据任务提供可重连的 SSE 事件流，使本机前端无需轮询即可及时获知任务进度，并在任务结束时收到当前公开的完整结构化结果或安全错误。

## Background

- 当前提交接口返回 `202 Accepted`、`job_id` 和 `status_url`，读取接口返回 `JobRecord`。
- Worker 和 LangGraph 均使用 `ainvoke()`；LLM 通过 Pydantic 结构化输出一次性返回，不能把未完成的 token 作为有效业务结果。
- Redis 是任务状态、修订、outbox 和 checkpoint 协调基础设施，API 与 Worker 已共享相同键前缀。
- 现有终态为 `succeeded`、`rejected`、`failed`；`waiting_input` 不是终态，提交回答后会产生下一修订。

## Requirements

- R1：保留现有提交、状态查询和回答接口的兼容行为。
- R2：新增按 `job_id` 订阅的 `text/event-stream` HTTP 接口。
- R3：事件必须使用稳定的类型化 JSON 契约，至少覆盖初始快照、状态变化、等待回答、成功结果、安全失败和心跳。
- R3.1：运行进度使用稳定业务阶段，不把 LangGraph 节点名暴露为公共 API；至少区分排队、解析、记忆加载、元数据生成、校验、持久化等阶段。
- R4：SSE 连接不得直接执行 LLM 或 LangGraph；Worker 继续拥有任务执行，API 只读取并转发事件。
- R5：客户端断线不得影响后台任务；重连时必须能恢复当前状态，并避免因进程内队列造成 API/Worker 跨进程丢失。
- R6：终态事件发送后正常结束连接；等待回答事件发送后保持连接，以便同一任务后续修订继续推送。
- R7：Redis 或客户端连接故障必须安全清理订阅资源，不泄漏任务输入、原始 DDL、模型提示或内部异常文本。
- R8：事件数据、保留时间、键空间和清理行为必须有界。
- R9：新增单元/集成测试覆盖事件序列、断线重连、等待回答后继续、终态关闭、404 和 Redis 故障。

## Acceptance Criteria

- [ ] `POST /api/v1/metadata/ddl-jobs` 的既有响应保持兼容，并提供可发现的事件流 URL。
- [ ] `GET /api/v1/metadata/ddl-jobs/{job_id}/events` 返回标准 SSE，事件包含单调事件 ID、事件类型、任务 ID、修订、时间和公开负载。
- [ ] 新连接先收到权威任务快照；后续状态变化无需轮询即可送达。
- [ ] 客户端携带 `Last-Event-ID` 重连时不会依赖单个 API 进程的内存状态。
- [ ] `waiting_input` 事件包含当前公开问题；提交回答后，同一连接可继续收到新修订事件。
- [ ] `succeeded`、`rejected` 或 `failed` 事件包含与 `JobRecord` 一致的公开结果/错误并关闭连接。
- [ ] 心跳可穿过空闲代理时段，且客户端断开后服务端及时释放 Redis 读取与生成器资源。
- [ ] 现有接口测试与新增 SSE 测试通过；Ruff、Pyright、`compileall` 和配置加载通过。

## Out of Scope

- 不把 Pydantic 结构化 LLM 结果改成未校验的逐 token 文本。
- 不新增 WebSocket。
- 不改变 DDL 任务状态机、问答修订语义或最终持久化结果。
- 不实现前端界面。

## Open Question

- 客户端重连时，是从权威当前快照继续，还是重放断线期间的全部阶段事件。
