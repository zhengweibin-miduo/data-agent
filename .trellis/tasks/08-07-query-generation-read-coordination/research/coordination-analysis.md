# Generation 读写协调取证

## 当前控制流

- `QueryReadinessAdapter.hold()` 把目标表映射到 `generation_lock_name()` 并调用
  仅有独占语义的 `MySQLDatabase.advisory_locks()`。
- Query 从最终 readiness/关系复核前持锁，跨 `EXPLAIN` 与完整流式 SELECT 后释放。
- accepted snapshot、Data Sync schema sync 和 reset 使用同名独占锁保护写侧提交。
- 因 `GET_LOCK()` 无共享模式，同表安全 Query 互相阻塞；Query 未映射获取异常。

## 推荐方案

MySQL 8.4 Locking Service 原生支持 shared read / exclusive write，单次多锁原子
获取、session 退出释放、commit/rollback 不释放。SQL functions 需要通过
`locking_service.so` bootstrap 注册，并在 API/worker startup capability probe。

官方文档：
<https://dev.mysql.com/doc/refman/8.4/en/locking-service.html>。

## 调用覆盖

- Query generation reader -> READ。
- accepted snapshot、schema sync、generation reset -> WRITE。
- schema、Binlog、Meta Projection 等无关命名锁保留 `GET_LOCK()`。

## 测试 seam

- infrastructure shared/exclusive contexts + startup probe；
- QueryReadinessPort 与 Query stream；
- Data Sync materialization adapter；
- accepted snapshot adapter；
- API、DDL worker、Data Sync worker startup。

真实 MySQL 必须证明两个 READ 同时进入、WRITE 排他、多 target 原子失败、事务不
释放和 session 退出释放。
