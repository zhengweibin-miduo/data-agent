# DDL 任务 SSE 流式输出设计

## 1. 目标与边界

新增一个只读 SSE 通道，把后台 DDL 任务的稳定业务阶段和公开任务投影实时送给本机浏览器。LLM 继续使用 Pydantic 结构化输出，Worker 继续拥有 LangGraph 执行，SSE 不改变任务状态机、问答修订和最终持久化语义。

本设计不输出模型 token，不公开 LangGraph 原始节点输入、检查点、DDL、提示词或异常文本。

## 2. HTTP 契约

- 提交响应 `DDLJobAccepted` 增加可选兼容字段 `events_url`，值为 `/api/v1/metadata/ddl-jobs/{job_id}/events`。
- 新增 `GET /api/v1/metadata/ddl-jobs/{job_id}/events`。
- 响应媒体类型为 `text/event-stream`，并设置 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`。
- 建立响应前先读取任务；不存在返回既有 `job_not_found` 404，Redis 不可用返回既有 503。
- 每次连接首先发送 `snapshot`。若快照已是终态，发送后关闭。
- 运行期间发送类型化事件；空闲时发送 `: heartbeat` 注释。
- Redis 在响应开始后失败时发送安全的 `stream_error` 事件并关闭，客户端可重连。

SSE 帧使用 Redis Stream ID 作为 `id:`，使用公共事件类型作为 `event:`，`data:` 是单行 JSON。心跳不携带业务数据或事件 ID。

## 3. 公共事件契约

公共模型放在 `ddl_metadata.models.jobs`：

- `JobEventStage`：稳定业务阶段枚举。
- `JobEventData`：`job_id`、`revision`、`attempt`、`status`、`stage`、`emitted_at`，以及按状态出现的 `questions`、`result`、`error`。
- 事件类型：
  - `snapshot`
  - `progress`
  - `waiting_input`
  - `succeeded`
  - `rejected`
  - `failed`
  - `stream_error`

稳定阶段映射：

| LangGraph/任务行为 | 公共阶段 |
|---|---|
| 提交、回答受理、重试回队 | `queued` |
| `parse_ddl` | `parsing` |
| `load_and_validate_memory` | `memory_loading` |
| `classify_metadata` | `metadata_generating` |
| `validate_metadata` | `metadata_validating` |
| `plan_metric_questions` | `question_planning` |
| 公开状态转换 | `waiting_input` |
| `generate_metrics` | `metric_generating` |
| `validate_metrics` | `metric_validating` |
| `build_memory_candidates` | `memory_building` |
| `persist_snapshot` | `persisting` |
| 公开终态 | `succeeded` / `rejected` / `failed` |

`await_metric_answers` 不直接发布公共阶段；只有问题已投影到 `JobRecord` 后才发布 `waiting_input`。

## 4. Redis 事件存储

在 `ddl_metadata.jobs.redis` 新增专职事件 Store，键为：

`{prefix}:job:{job_id}:events`

每条记录只保存公共事件类型和安全 JSON 数据。写入使用 `XADD MAXLEN ~ <configured_limit>`，并刷新与 `result_retention_seconds` 一致的 TTL。事件条数和生存期均有界。

`DDLJobStore` 继续是 API、Worker 和 workflow 的应用门面：

- 状态变更成功后发布对应公开事件。
- 提交、回答受理和重试回队发布 `queued`。
- Worker 消费 LangGraph `stream_mode="tasks"`，只读取任务开始事件中的节点名，映射为稳定业务阶段后发布；绝不转发 task input/result/error。
- 事件发布是可观察性副作用。单次进度事件写入失败只记录安全告警，不改变已经成功的业务状态或使图执行失败。

状态 Hash 仍是权威来源。Redis Stream 是有界通知日志，不成为第二个任务状态源。

## 5. 执行与重连数据流

```text
Browser ──POST job──> API ──state/outbox──> Redis
Browser <──202 + events_url── API

Browser ──GET events──> API
API ──read JobRecord──> Redis
Browser <──snapshot── API

Worker ──astream(tasks)──> LangGraph
Worker ──stable stage event──> Redis Stream
API ──XREAD block──> Redis Stream
Browser <──progress/waiting/terminal── API
```

断线不会取消 Worker。重连不重放旧阶段：API 读取当前 `JobRecord` 和当前事件流尾 ID，发送一个权威 `snapshot`，再从该 ID 之后阻塞读取。快照读取和后续事件之间允许重复但不允许遗漏当前状态；客户端按快照覆盖本地状态。

每个阻塞读取超时后，API 重新读取权威 `JobRecord`：

- 投影发生变化但事件写入缺失时，发送修复 `snapshot`。
- 已进入终态时发送快照并关闭。
- 未变化时发送心跳。

这使状态变更与事件追加不必扩张现有 Lua 协议，同时可以修复两步写入窗口。

## 6. 配置

- `api.sse_heartbeat_seconds`：SSE 阻塞读取和心跳间隔。
- `redis.event_stream_max_events`：每个任务事件流的近似最大条数。
- 事件 TTL 复用 `redis.result_retention_seconds`，不新增重复保留配置。

配置继续使用 Pydantic 严格校验并记录到 `conf/app_config.yaml`。

## 7. 错误与安全

- 建立流之前复用 FastAPI 的业务错误和 Redis 503 映射。
- 建立流之后仅发送固定 `stream_error` 代码、阶段和 `retryable=true`，不发送异常消息。
- 只序列化 `JobRecord` 已公开的字段与稳定阶段。
- `request.is_disconnected()` 和异步生成器取消用于停止读取；Redis `XREAD` 使用共享连接池中的有界阻塞调用，不创建需手工退订的 Pub/Sub 资源。
- 多个浏览器可独立读取同一 Stream，不消费或删除彼此事件。

## 8. 兼容与回滚

- 现有 POST/GET/answers 路由和模型字段保持不变；`events_url` 是新增字段。
- Redis Hash、dispatch/waiting/cleanup 键和 Lua 状态协议不改变。
- LangGraph 从 `ainvoke` 改为完全消费 `astream(stream_mode="tasks", version="v2")`，仍使用 `durability="sync"`，最终投影逻辑保持不变。
- 回滚时删除事件 Store、SSE 路由、事件模型、阶段映射和新增配置，再恢复 `ainvoke`；权威任务数据无需迁移。

## 9. 关键权衡

- 选择 Redis Streams 而非进程内队列：API 与 Worker 可跨进程，且事件 ID、阻塞读取和有界历史由 Redis 提供。
- 选择当前快照续接而非完整历史重放：避免界面在重连后倒退到过期阶段。
- 选择 LangGraph task-start 事件而非 LLM token：满足真实进度可见性，同时保留结构化输出校验。
- 不把事件追加塞入现有状态 Lua：保持状态协议稳定，由心跳时的权威快照修复两步写入窗口。
