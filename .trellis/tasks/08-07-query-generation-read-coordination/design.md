# 设计：MySQL generation 共享读与独占写协调

## 设计目标与不变量

Query 读取和 Data Sync 重建必须满足以下不变量：

1. 同一 DW target 的多个 Validated Query 可以并发读取。
2. schema 同步、accepted Meta Snapshot 发布和 generation reset 对读取保持独占。
3. 多 target 获取要么一次全部成功，要么一个也不持有。
4. 事务提交、回滚不提前释放协调权；取消、连接异常或进程退出不会泄漏锁。
5. Locking Service functions 缺失时，所有相关进程启动即失败关闭。

## Module、Interface 与 Seam

深化既有 `MySQLDatabase` module，在 `advisory_locks()` 旁增加原生读写锁
interface：

```python
MySQLDatabase.shared_service_locks(names, *, timeout_seconds)
MySQLDatabase.exclusive_service_locks(names, *, timeout_seconds)
MySQLDatabase.check_locking_service()
```

调用方只知道共享读或独占写、资源身份和等待预算。SQL function、namespace、参数
绑定、原子多锁获取、错误分类和 owner 连接清理由 implementation 隐藏。这个 seam
同时服务 Query、Data Sync、accepted snapshot 和真实 MySQL 测试，提供 leverage 和
locality。

既有 `advisory_locks()` 继续承载 schema、Binlog、Meta Projection 等普通独占锁，
不机械迁移无关调用方。

## MySQL Locking Service 协议

MySQL 8.4 Locking Service 明确定义：read lock 可被其他 read session 共享，write
lock 排斥 read/write；一次调用可获取多个锁并具有原子性；锁在 session 结束时自动
释放，commit/rollback 不释放。SQL interface 由 loadable functions 提供：

- `service_get_read_locks(namespace, lock..., timeout)`；
- `service_get_write_locks(namespace, lock..., timeout)`；
- `service_release_locks(namespace)`。

官方依据：
<https://dev.mysql.com/doc/refman/8.4/en/locking-service.html>。

implementation 使用专用连接和固定 generation namespace；资源名继续复用稳定、
二进制区分且不超过 64 字节的 `generation_lock_name()`。单次 SQL 调用传入排序、去重
后的全部 target，避免逐把锁造成的部分获取和多表死锁。释放函数会释放当前 session
在该 namespace 的全部锁，因此每个上下文必须独占一条连接且不能嵌套复用。

获取 timeout/deadlock 映射为 `AdvisoryLockUnavailableError`；function 缺失或形态
错误映射为启动能力错误；release 失败沿用连接 invalidate 与“不覆盖活动业务异常”
规则。

## 调用流

### Query 共享读

`QueryApplication.stream()` 继续通过 `QueryReadinessPort.hold()` 进入协调区：

```text
Validated Query
  -> shared generation service locks
  -> accepted relationship fingerprint recheck
  -> final readiness recheck
  -> EXPLAIN
  -> complete streamed SELECT
  -> release read locks
```

`QueryReadinessAdapter` 是 MySQL 实现 adapter。获取 timeout/deadlock 映射为稳定、
可重试的 `DataAgentError`，使首事件前走 HTTP 409，响应开始后走类型化
`stream_error`，不再暴露裸基础设施异常。

### Data Sync 和 accepted snapshot 独占写

以下 generation owner 改用 exclusive interface：

- `MySQLMaterializationAdapter.synchronize_schema()`；
- `MySQLMaterializationAdapter.reset_generation()`；
- accepted Meta Snapshot 发布事务覆盖的全部 target。

独占上下文继续包围既有业务 Session，事务 commit/rollback 完成后才 release。
普通 CDC DML、schema lock、Binlog source lock 与 metadata-index lock 保持原实现。

## 能力安装与启动门禁

新增幂等的全新环境 bootstrap SQL，使用 root 注册 `locking_service.so` 的三个
functions。Docker entrypoint 自动执行；CI service 不挂载初始化目录，因此 CI 在
建库前显式执行同一脚本。

`MySQLDatabase.check_locking_service()` 使用专用连接执行无副作用的 release probe。
API lifespan、DDL worker startup、Data Sync worker startup 在装配业务 module 前
await 该 probe。仓库不在运行时以高权限安装 function，也不静默降级回独占
`GET_LOCK()`。

已有 volume 若缺 functions，需要按 V1 运维方式重建 MySQL 或由管理员显式执行
bootstrap；没有业务数据 migration。

## TDD Seam

实现通过以下公共 seam 验证：

1. infrastructure seam：shared/exclusive context managers 与 capability probe，
   验证真实共享、写排他、原子多锁、事务不释放、取消和 session 关闭释放。
2. Query adapter/application seam：`QueryReadinessPort.hold()` 与公开 Query stream，
   验证共享模式、锁竞争错误映射和完整读取范围。
3. Data Sync adapter seam：`synchronize_schema()` / `reset_generation()`，验证独占
   模式覆盖事务提交。
4. accepted snapshot adapter seam：发布事务对全部 target 使用一次原子独占获取。
5. process startup seam：API/DDL worker/Data Sync worker 在 capability 缺失时不进入
   业务循环。

测试不读取私有连接状态或 mock module 内部函数；MySQL 集成测试用独立连接观察
实际临界区行为。

## 备选方案与取舍

### 删除或缩短 generation lock

拒绝，会重新打开 readiness 与执行之间的 generation 替换竞态。

### 固定 reader slots 模拟读写锁

拒绝。虽然不需要 server function，但带来人为并发上限、槽位碰撞和 writer
starvation；MySQL 8.4 已提供原生共享/独占锁。

### 持久化 reader lease / Redis 分布式锁

拒绝。前者需要 migration、heartbeat 和 fencing，后者在租约提前失效时无法与
MySQL 写事务证明互斥。

### REPEATABLE READ generation snapshot

可避免慢客户端长期阻塞 reset，但必须把 readiness、关系复核、只读连接和
executor 重塑为一个更深的执行 module，并处理 copy-style ALTER 的
`ER_TABLE_DEF_CHANGED`。这是后续优化候选，不是当前并发缺陷的最小修复。

### MySQL Locking Service（采用）

原生真共享读/独占写、单次原子多锁、跨进程且连接崩溃自动清理；代价是 MySQL
server 必须安装官方 loadable functions。

## 兼容、发布与回滚

- 新旧 generation 协议不能混跑；合入 PR #85 后 API、worker 和接受快照的进程
  必须统一版本重启。该 PR 尚未合入，不实现双协议兼容。
- 无业务数据 migration；回滚代码并统一重启即可。若完全回滚，可由管理员保留或
  卸载未再使用的 global functions。
- function 未安装时启动门禁失败，不允许退回会串行读者的旧协议。

## 残余风险

- 慢速且不主动断开的客户端仍可延长 read lock，并延迟 generation 写者；写者按
  现有有限 timeout 返回可重试繁忙并重试。本任务不重新引入会截断完整 NDJSON 的
  响应级固定 timeout。
- 官方 metadata lock 调度给予写请求更高优先级，但不承诺应用级严格 FIFO。
- global loadable functions 是 MySQL 实例能力，启动 probe 与部署说明必须覆盖所有
  API/worker 进程。
