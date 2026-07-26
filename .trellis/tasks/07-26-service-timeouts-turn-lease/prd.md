# 补齐外部服务超时与会话轮次租约

## Goal

消除两类"永久挂起"故障：外部服务无响应时调用方无限等待，以及客户端在轮次中途
崩溃后会话永久无法开启新轮次。

## Background

架构审查记录的相关问题编号（用于追溯）：

- P0-9：只有 LLM 客户端配置了超时与重试。Redis 无 `socket_timeout`（TCP 半开连接
  会让 API 无限挂起，且业务异常处理器兜不住"不返回"的调用）、Elasticsearch 无
  `request_timeout`、Qdrant 无 `timeout`、TEI 无 `timeout`。记忆索引写路径因此完全
  没有超时保护——前序任务已把外部调用移出 MySQL 事务，但一次挂起的 TEI 调用仍会
  永久占用 dispatcher 的 cron 槽位。
- P0-6：`active_turn_uid` 无租约、无超时，唯一清除路径是 `complete_turn`。调用方在
  `start_turn` 与 `complete_turn` 之间崩溃且丢失 `turn_uid`，该会话此后所有新轮次
  永远返回 409 `conversation_busy`，只能人工改库。系统里其它每个"占用"都有租约。

## Requirements

### 外部服务超时

- 每个外部客户端都必须显式声明连接与请求超时，取值来自统一配置而非库默认值。
- Redis 的 `socket_timeout` 必须严格大于 SSE 阻塞读取时长，否则正常的空闲心跳
  会被误判为套接字超时而中断事件流；该约束必须由配置校验强制，不能只写在注释里。
- Elasticsearch 保留有界重试；Qdrant、TEI 只要求超时，失败重试由既有 outbox
  退避负责，不叠加客户端层重试。
- 不改变既有的客户端生命周期契约（`initialize`/`get_client`/`close` 的幂等语义）。

### 会话轮次租约

- 活动轮次门禁必须有超时：超过配置租约仍未完成的轮次可被新轮次抢占。
- 抢占判定必须由数据库端时钟完成，不得用 Python naive 时间与数据库 `now()` 比较。
- 未超时的在途轮次仍必须返回 409 `conversation_busy`，同一 `turn_uid` 的幂等回放
  行为保持不变。
- 不新增数据库列：`start_turn` 写入 `active_turn_uid` 时同时写 `updated_at`，
  该列即轮次占用起点。

## Constraints

- 不引入新的外部依赖。
- 新增配置项必须在 `conf/app_config.yaml` 显式给值，并带中文字段描述。
- 不做审查中记录的分层重构（全局单例改注入、配置惰性加载、worker 组合根上移）。
- 客户端超时参数必须使用当前锁定依赖版本真实支持的参数名。

## Acceptance Criteria

- [x] Redis、Elasticsearch、Qdrant、TEI 四个客户端都从配置注入显式超时。
- [x] 配置校验拒绝 `redis.socket_timeout_seconds <= api.sse_heartbeat_seconds`，
      并有单元测试覆盖。
- [x] 有单元测试断言四个客户端把配置超时真实传给底层库。
- [x] 超过租约的在途轮次可被新轮次抢占；未超时的仍返回 409 `conversation_busy`。
- [x] 抢占判定的 SQL 使用数据库端时间函数，有测试断言渲染结果。
- [x] README 记录的基础质量门禁全部通过。
