# 实施计划：MySQL generation 共享读与独占写协调

## 1. Locking Service bootstrap 与 capability tracer bullet

- 先为 API、DDL worker、Data Sync worker startup seam 增加失败测试：functions
  probe 失败时不得进入业务装配或循环。
- 新增全新环境 bootstrap SQL 注册 `locking_service.so` 的三个 functions，并让
  Docker entrypoint 与 CI 共用该文件。
- 在 `MySQLDatabase` 增加 capability probe，并接入三个进程启动点。

## 2. Infrastructure 读写锁 tracer bullet

- 在 `backend/tests/unit/infrastructure/test_mysql.py` 先增加失败测试，固定参数
  绑定、名称排序去重、单次调用、timeout/deadlock 分类、release、取消和清理失败。
- 在 `backend/tests/integration/infrastructure/test_mysql.py` 先增加真实测试：两个
  独立连接的 READ 同时进入；WRITE 与 READ/WRITE 互斥；多 target 原子失败不残留；
  commit 不释放，context/session 退出释放。
- 在 `backend/src/infrastructure/mysql.py` 实现最小 shared/exclusive context
  managers，保持原 `advisory_locks()` 行为不变。

## 3. Query vertical slice

- 在 Query adapter/application 公共 seam 先写失败测试，证明 Query 使用 READ，
  同表两个请求能并发到达 executor，timeout/deadlock 映射稳定可重试错误。
- 更新 `QueryReadinessAdapter.hold()`；最终 readiness、关系复核、`EXPLAIN` 和完整
  流式读取范围不变。

## 4. Data Sync 与 accepted snapshot vertical slice

- 先更新 adapter seam 测试，证明 schema sync、generation reset 和 accepted
  snapshot 使用 WRITE，并保持到事务提交/回滚完成。
- accepted snapshot 的多个 target 必须通过一次 function 调用原子取得。
- 普通非 generation 命名锁继续使用 `advisory_locks()`。

## 5. 规范与部署一致性

- 更新 `.trellis/spec/backend/database-guidelines.md`、
  `.trellis/spec/backend/query-guidelines.md`、Docker MySQL README 和 CI 初始化步骤。
- 搜索全部 `generation_lock_name()` 调用，确认 Query 读者与所有 generation 写者均
  使用 Locking Service；不改变 metadata-index 等无关锁。
- 明确已有 volume 的人工 bootstrap/重建要求，不创建隐式 migration。

## 6. 验证门禁

```text
node/SQL bootstrap 静态检查与 workflow YAML 检查
cd backend && uv run pytest tests/unit/infrastructure/test_mysql.py tests/unit/infrastructure/test_logging_lifecycle.py tests/unit/infrastructure/test_client_timeouts.py -q
cd backend && uv run pytest tests/unit/query tests/unit/data_sync/adapters/test_mysql.py tests/unit/ddl_metadata/test_snapshots.py -q
cd backend && uv run pytest tests/integration/infrastructure/test_mysql.py tests/integration/query/test_mysql_executor.py -q
cd backend && uv run pytest -m "not tei" -q
cd backend && uv run ruff check src tests
cd backend && uv run pyright src tests
cd backend && uv run python -m compileall -q src tests
cd backend && uv run python -m settings
cd backend && uv build
git diff --check
```

真实 MySQL 或 loadable function 不可用时必须记录原因，不得以 fake 测试替代并
声称原生 read/write 语义通过。

## 7. Review、提交与推送

- 使用 Trellis check agent 复核 spec、启动数据流、锁调用覆盖和完整质量门禁。
- 主代理复核完整 diff、PR #85 head 的远端干预关系与实际验证结果。
- 创建一个中文提交并普通推送
  `git push origin HEAD:feature/query-sql-flow-20260805`；禁止 force-push，不创建
  新 PR。推送后核验远端 SHA 与 PR head。

## 回滚点

- capability probe 或真实 MySQL READ/WRITE 测试未通过前，不接入业务调用方。
- 任一非原子多锁、提前释放、异常泄漏或未映射 Query 错误会回滚到规划。
- 若目标 MySQL distribution 缺 `locking_service.so`，停止实现并重新评审 snapshot
  方案，不降级成固定 slots 或独占 GET_LOCK。
