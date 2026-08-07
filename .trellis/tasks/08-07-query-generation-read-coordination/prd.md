# 实现查询与同步的安全代次读写协调

## Goal

在保持 DW generation 一致性的前提下，允许多个只读 Query 并发读取同一目标
表，同时保证 schema 同步和 generation reset 对这些读取保持独占，消除当前
单把 MySQL `GET_LOCK` 将所有同表查询串行化的问题。

## Background

- PR #85 当前在最终 readiness 复核、`EXPLAIN` 和完整流式读取期间持有目标表
  generation lock，正确封闭了同步重建竞态。
- Query 与 Data Sync 都使用同名 MySQL `GET_LOCK`；该锁只有独占模式，因此
  两个安全读请求也会互斥，并可能把锁等待异常暴露为 500。
- 直接移除或提前释放锁会重新允许 generation 在验证与读取之间被替换，产生
  空集或部分结果，不能接受。

## Requirements

- 提供一个跨进程的 generation 协调 module，其小接口明确区分共享读与独占写，
  并隐藏 MySQL 命名锁、排序、超时和清理细节。
- 使用 MySQL 8.4 Locking Service 的原生 read/write 语义；同一调用中的多 target
  必须原子获取，任一失败不得残留部分锁。
- 多个 Query 可以同时持有同一目标表的共享读权限；schema 同步和 generation
  reset 必须等待全部既有读者退出，并阻止新读者越过已经进入的写者。
- Query 跨多个目标表时，协调资源必须采用稳定全局顺序，避免读读、读写和写写
  死锁。
- 最终 readiness 复核、权威关系快照复核、`EXPLAIN` 和完整流式读取继续位于同一
  共享读协调区内。
- Data Sync 的 schema 同步与 generation reset 继续在事务提交完成前持有独占写
  协调权。
- 获取超时必须映射为稳定、可重试的业务结果或既有资源繁忙错误，不能泄露为
  未映射 500。
- 取消、异常和清理失败不得泄漏协调资源，也不得覆盖原始查询或同步异常。
- API、DDL worker 和 Data Sync worker 启动时必须探测 Locking Service SQL
  functions；缺失时 fail closed，不能到首个业务请求才返回未知 500。
- Docker/CI 的全新 MySQL 初始化必须安装 `service_get_read_locks`、
  `service_get_write_locks` 和 `service_release_locks`；不为已有数据卷增加隐式运行时
  migration。
- 不新增数据迁移；V1 没有旧协调状态需要迁移或清理。
- 保持只读账号权限、Validated Query 门禁、Meta Snapshot 权威性和 NDJSON 契约
  不变。

## Acceptance Criteria

- [ ] 两个同表 Query 能在协调 module 的共享读 seam 上同时进入临界区。
- [ ] 独占写者等待已有读者，并在等待期间阻止后续新读者插队。
- [ ] 读者在独占写者持有期间等待或得到稳定可重试结果，不能读到重建中代次。
- [ ] 多表查询使用稳定顺序，测试覆盖相反输入顺序且不发生死锁。
- [ ] 单次多锁获取失败时原子失败，不能留下部分 generation 锁。
- [ ] Query 应用 seam 覆盖同表并发、协调超时映射和异常清理。
- [ ] Data Sync adapter seam 覆盖 schema/reset 的独占持有范围直到提交。
- [ ] MySQL 集成测试证明跨独立连接的共享读、独占写和释放语义；服务不可用时
  如实报告而不声称通过。
- [ ] 启动探测在 functions 缺失时阻止 API/worker 接受业务，在已安装时通过。
- [ ] Query、Data Sync、infrastructure 相关单元/集成测试，以及 Ruff、Pyright、
  `compileall`、配置检查、构建和 `git diff --check` 通过。
- [ ] 仅把本任务提交推送回现有 PR head
  `feature/query-sql-flow-20260805`，不创建新 PR。

## Out of Scope

- 不改变查询语义、SQL 门禁或业务字段绑定规则。
- 不引入业务数据迁移、持久化 reader lease 表或运行时自动安装 SQL function。
- 不以“降低锁超时”或“删除 generation 锁”作为并发方案。
