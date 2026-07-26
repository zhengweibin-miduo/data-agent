# 技术设计

## 一、DDL 任务活动索引与停滞巡检（P0-1）

### 现状

任务的可恢复调度依赖两个 Redis 结构：`{prefix}:dispatch`（已受理未入队）与
`{prefix}:waiting`（等待人工回答）。两者都不覆盖"已入队但被 arq 放弃"的任务：
`job_runner` 的每次 `Retry` 都消耗 arq `max_tries=3`，耗尽后 arq 静默丢弃
（`keep_result=0`），而任务 Hash 停留在 `pending`/`running`，dispatch outbox 已被
`zrem` 清空。任务 Hash 只有终态才设 TTL，非终态任务因此永久存在。

### 方案：活动任务索引 + 停滞巡检 cron

新增键 `{prefix}:active`：ZSET，member 为 `job_id`，score 为最后一次状态推进的
Redis 服务端时间（秒）。选择服务端 `TIME` 而非客户端时间，与 TRANSITION 脚本中
checkpoint cleanup outbox 的既有做法保持同一时钟。

维护点（全部在既有 Lua 脚本内原子完成，不新增网络往返）：

| 脚本 | 变更 |
| --- | --- |
| `SUBMIT` | 受理成功后 `ZADD active <now> job_id` |
| `TRANSITION` | 终态 `ZREM active job_id`；非终态 `ZADD active <now> job_id` |
| `ANSWER` | 受理回答（返回 1）`ZADD`；过期拒绝（返回 -1）`ZREM` |

巡检 `reap_stalled_jobs`（新 cron，每分钟）：

1. `ZRANGEBYSCORE active -inf (now - stall_threshold)`，有界取前 N 个。
2. 逐项读取任务 Hash，按状态分派，**每项独立 try/except**：
   - Hash 缺失或已是终态 → `ZREM`（孤儿自愈）。
   - `waiting_input` → 由 `expire_waiting` 负责，仅刷新 score 避免重复扫描。
   - `pending` → 重新写入 dispatch outbox（member 为 `job_id:revision`），刷新 score。
   - `running` 且 `attempt < max_job_attempts` → CAS `running -> pending`，再写
     dispatch outbox。
   - `running` 且 `attempt >= max_job_attempts` → CAS `running -> failed`，
     错误码 `job_stalled`。
3. 重新投递依赖 arq 确定性 job id（`{prefix}:{job_id}:{revision}`）去重，因此对仍在
   arq 队列中的任务是幂等空操作。

**停滞阈值** = `redis.worker_job_timeout_seconds + redis.job_stall_grace_seconds`。
必须严格大于 arq 的 `job_timeout`（等于 `worker_job_timeout_seconds`），保证被巡检
回退的 `running` 任务对应的 arq 执行已被超时取消，不会双跑。`running` 任务在整个
执行期间不刷新 score，这正是停滞信号的来源，阈值因此不能小于单次执行上限。

### 兼容性

- 新键只增不改，旧数据无需迁移。已存在的非终态任务不在索引中，且**不会自然过期**
  ——非终态任务 Hash 不设 TTL，只有终态转换才写入保留期。因此 worker 启动时执行一次
  幂等补录（`backfill_active_index`）：按游标扫描任务 Hash 键，把仍处于非终态的任务
  写入活动索引，使停滞巡检同样覆盖升级前遗留与滚动发布期间由旧实例受理的任务。
- `JobRecord` 公开契约不变。

## 二、来源租约窗口与属主校验（P0-4）

- `AppSettings.validate_source_lease_window` 的 `minimum` 由 `max(...)` 改为求和：
  租约在激活开始时续期一次，随后可能先执行满 `worker_job_timeout` 再进入等待
  `waiting_timeout`，两段是串联而非互斥。当前 `conf/app_config.yaml`
  （3600 >= 600 + 1800）已满足新约束，无需改配置值。
- `ANSWER` 脚本的成功分支把无条件 `EXPIRE KEYS[4]` 改为先比对
  `GET KEYS[4] == HGET KEYS[1] job_id`，与同脚本超时分支及 `RENEW`/`RELEASE`
  的既有属主校验保持一致。租约键不存在或属于他人时不续期，由 worker 激活时的
  `renew` 失败路径按既有语义处理。

## 三、记忆索引 dispatcher 三段式（P0-2）

### 现状

`MemoryIndexDispatcher.dispatch()` 在单个 `MySQLDatabase.session()` 内完成
"领取（`FOR UPDATE SKIP LOCKED`）→ 逐项 ES/Qdrant/TEI → 确认/退避"，行锁跨越所有
外部网络调用。所有权威写路径都要 `set_desired_state` 同一批行，因此外部服务变慢
会直接阻塞用户写事务。

### 方案

```
阶段一（短事务）：claim_outbox(limit) 领取并写租约 available_at = now() + lease
                  → 提交，释放行锁
阶段二（无事务）：逐项 ES / Qdrant / TEI 外部调用
阶段三（短事务，每项独立）：acknowledge_outbox(item) 或 retry_outbox(item, ...)
```

- 领取租约用 `available_at` 表达，不新增列：`claim_outbox` 在同一事务内
  `UPDATE ... SET available_at = now() + INTERVAL <lease> SECOND`。并发 dispatcher
  的 `available_at <= now()` 条件因此不会重复领取；进程崩溃时租约到期后行自动
  重回可领取状态，不需要额外的死锁恢复。
- 领取不递增 `attempts`：`attempts` 保留"外部调用失败次数"的语义，租约到期重领
  不应消耗死信预算。这意味着无限崩溃循环不会被死信拦截，但那属于进程健康问题，
  由停滞告警而非死信覆盖。
- `retry_outbox` 补齐 `operation` 与 `projection_version` 条件，与
  `acknowledge_outbox` 的全字段匹配对齐：迟到的 worker 不得改写已被新期望覆盖的行。

## 四、UPSERT 收敛语义与重建竞态（P0-3）

### UPSERT 收敛

dispatcher 处理 `UPSERT` 时的判定改为：

```
projection is None or projection.status is not ACTIVE  → index.delete(uid)
否则                                                    → index.upsert(...)
```

原实现在这两种情况下要么写入失效内容（status=DELETED 仍带完整 `memory_text`），
要么直接确认而不做任何事（行已被物理清理）。后者与 `purge_ready_user_memories`
"outbox 清空即可硬删"的门禁叠加，会让派生索引中的用户原文永久残留。收敛为删除
后，UPSERT 的语义统一为"让派生索引收敛到权威状态"，对两种失效情况都安全。

### 重建竞态

`enqueue_rebuild(uids)` 改为：

1. 在当前事务内 `SELECT uid FROM agent_memory WHERE uid IN (...) AND
   status = 'active' FOR UPDATE`，得到实际锁定的 active 集合。
2. 只对该集合 `UPDATE ... SET projection_version = <new>`。
3. 只对该集合 `set_desired_state(..., UPSERT)`。

加锁使两个方向的交错都收敛到正确终态：删除事务先提交时，重建的 `FOR UPDATE`
查询看不到该行（status 已变），不会为它生成 UPSERT；重建先持锁时，删除事务阻塞
到重建提交后才写 DELETE 期望，最终期望仍是 DELETE。

`scan_active` 保持无锁游标扫描（重建的取数阶段不应长时间持锁），竞态由
`enqueue_rebuild` 的二次锁定复核收敛——这与仓库里"入口快速校验 + 事务内锁定复核"
的既有双重检查模式一致。

## 五、退避时间与死信（P1-12、P1-13）

- `retry_outbox` 的 `available_at` 由
  `datetime.now(UTC).replace(tzinfo=None) + timedelta(...)` 改为数据库端
  `func.timestampadd(text("SECOND"), delay, func.now())`，与 `claim_outbox` 的
  `available_at <= func.now()` 使用同一时钟。`ConversationRepository.claim_extractions`
  已是数据库端算法，本次让 outbox 与之对齐。
- `claim_outbox` 增加 `attempts < memory.outbox_max_attempts` 条件。超限行保留在表中
  （仍被 `pending_targets` 遮蔽，避免陈旧命中；也仍阻塞 `purge_ready_user_memories`
  的物理清理，这是安全方向），并在 dispatcher 每轮统计后以 warning 暴露。

## 六、错误详情默认值（P0-5）

`DataAgentError.__init__` 的 `self.details = details or {"message": message}` 改为
`self.details = dict(details) if details else {}`。所有显式传 `details` 的调用点
行为不变；未传的调用点不再把内部 message 投影到 HTTP 响应与事件流。

已核查消费方：`application.py` 的错误处理器、`store.py` 的 `error_json`、
`nodes.py` 的失败投影都只做透传，没有读取 `details["message"]` 的分支。

## 七、新增配置项

| 键 | 类型 | 值 | 说明 |
| --- | --- | --- | --- |
| `redis.job_stall_grace_seconds` | int > 0 | 120 | 停滞判定在任务超时之上的额外宽限 |
| `redis.max_job_attempts` | int > 0 | 3 | 任务被巡检重新激活的尝试上限 |
| `memory.outbox_claim_lease_seconds` | int > 0 | 300 | dispatcher 领取租约秒数 |
| `memory.outbox_max_attempts` | int > 0 | 10 | 索引 outbox 死信阈值 |

## 八、测试策略

- Lua 协议（活动索引维护、ANSWER 属主校验）：扩展现有
  `tests/unit/ddl_metadata/jobs/redis/test_job_stores.py` 的 fake Redis 用例。
- 停滞巡检三条分支（pending 重投、running 回退、超限失败）+ 孤儿自愈：新单测。
- dispatcher：新单测，用可记录调用顺序的 fake session/index 断言
  "外部调用发生在事务提交之后"，以及 UPSERT 对失效行收敛为 delete。
- `enqueue_rebuild`：单测断言只为锁定的 active 子集生成期望。
- 配置校验：扩展 `tests/unit/test_settings.py`，断言 max 语义被求和语义取代。
