# 技术设计

## 一、外部客户端超时

已用当前锁定依赖核对参数名（`uv run python -c "inspect.signature(...)"`）：

| 客户端 | 注入参数 | 取值 |
| --- | --- | --- |
| `Redis.from_url` | `socket_timeout`、`socket_connect_timeout`、`health_check_interval` | 30 / 5 / 30 |
| `AsyncElasticsearch` | `request_timeout`、`max_retries`、`retry_on_timeout` | 10 / 2 / True |
| `AsyncQdrantClient` | `timeout` | 10 |
| `AsyncInferenceClient` | `timeout` | 30 |

### Redis 超时与 SSE 阻塞读取的耦合

`job_events.py` 的 SSE 循环用 `xread(block=sse_heartbeat_seconds * 1000)` 做有界
阻塞读取，空闲时**正常**会阻塞满整个心跳间隔。redis-py 的 `socket_timeout` 是
单次命令的读取超时，因此 `socket_timeout <= sse_heartbeat_seconds` 会把正常心跳
变成 `TimeoutError`，直接打断事件流。

这条耦合必须由 `AppSettings` 的跨字段校验强制：

```python
if self.redis.socket_timeout_seconds <= self.api.sse_heartbeat_seconds:
    raise ValueError("redis.socket_timeout_seconds 必须大于 api.sse_heartbeat_seconds")
```

默认配置 30 > 15 满足约束。该校验同时保证以后调大心跳（上限 300 秒）时不会
静默破坏 SSE。

### 重试层次

Elasticsearch 保留客户端层有界重试（`max_retries=2` + `retry_on_timeout`），因为
它的读路径（记忆检索）没有 outbox 兜底。Qdrant 与 TEI 只设超时不加客户端重试：
它们的写路径已由记忆索引 outbox 的指数退避与死信上限负责，叠加两层重试会让
单次 cron 周期的最坏耗时不可预测。

## 二、会话轮次租约

### 为什么不加列

`start_turn` 在同一条 UPDATE 中写入 `active_turn_uid=turn_uid, updated_at=func.now()`
（`conversation/repository.py`），因此 `agent_conversation.updated_at` 恰好就是轮次
占用起点，不需要新增 `active_turn_started_at`。轮次进行期间只有异步摘要写入会再次
推进 `updated_at`，其效果是把抢占时刻往后推——偏向"不误抢占正在进行的轮次"，
是安全方向。

### 判定必须在 SQL 内完成

事务已通过 `get(..., for_update=True)` 锁定会话行，因此不存在竞态；但
`conversation["updated_at"]` 是数据库 naive 时间，用 `datetime.now(UTC)` 与它相减
会重新引入"应用时钟与数据库会话时区不一致"的缺陷（前序任务刚在 outbox 退避上
修掉同一根因，并已写入 `database-guidelines.md`）。因此可抢占性用一次 SQL 判定：

```python
claimable = (
    await session.execute(
        select(agent_conversation.c.id).where(
            agent_conversation.c.id == conversation_id,
            agent_conversation.c.user_id == user_id,
            or_(
                agent_conversation.c.active_turn_uid.is_(None),
                agent_conversation.c.active_turn_uid == turn_uid,
                agent_conversation.c.updated_at
                <= func.timestampadd(text("SECOND"), -lease_seconds, func.now()),
            ),
        )
    )
).one_or_none()
if claimable is None:
    raise DataAgentError("conversation_busy", ..., http_status=409)
```

放在原门禁的位置（锁定之后、幂等回放检查之前），因此：

- 无活动轮次、或同一 `turn_uid` 回放 → 与现状完全一致。
- 有其他活动轮次且未超租约 → 仍然 409 `conversation_busy`。
- 有其他活动轮次且已超租约 → 放行，后续 UPDATE 用新 `turn_uid` 覆盖门禁。

被抢占的旧轮次此后调用 `complete_turn` 会因 `active_turn_uid` 不再匹配而按既有
语义失败，不会覆盖新轮次的工作——这是现有 `complete_turn` 已实现的检查，无需改动。

## 三、新增配置项

| 键 | 类型 | 值 |
| --- | --- | --- |
| `redis.socket_timeout_seconds` | float > 0 | 30 |
| `redis.socket_connect_timeout_seconds` | float > 0 | 5 |
| `elasticsearch.request_timeout_seconds` | float > 0 | 10 |
| `elasticsearch.max_retries` | int >= 0 | 2 |
| `qdrant.timeout_seconds` | int > 0 | 10 |
| `tei.request_timeout_seconds` | float > 0 | 30 |
| `conversation.turn_lease_seconds` | int > 0 | 600 |

## 四、测试策略

- 四个客户端各一条单测：初始化后从客户端实例读回真实生效的超时配置，
  断言等于配置值（而不是断言调用了构造函数）。
- 配置校验：`socket_timeout <= sse_heartbeat` 必须被拒绝；边界相等也拒绝。
- 轮次租约：用语句渲染断言可抢占性判定使用 `timestampadd(SECOND, -N, now())`，
  并用记录型 Session 覆盖"未超租约仍 409"与"超租约放行"两条分支。
