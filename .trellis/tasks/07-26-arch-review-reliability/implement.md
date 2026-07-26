# 执行计划

按依赖顺序分四组推进，每组结束跑一次质量门禁，可独立回滚。

## 组一：低风险独立修复

- [x] 1.1 `src/data_agent/errors.py`：`details` 默认空字典。
- [x] 1.2 `src/data_agent/settings.py`：`validate_source_lease_window` 的 `minimum`
      由 `max(...)` 改为 `worker_job_timeout_seconds + waiting_timeout_seconds`。
- [x] 1.3 `src/data_agent/ddl_metadata/jobs/redis/scripts.py`：`ANSWER` 成功分支
      续期前校验租约属主。
- [x] 1.4 `src/data_agent/ddl_metadata/jobs/store.py`：`expire_waiting` 循环逐项
      try/except，单项失败记录后继续。
- [x] 1.5 测试：`tests/unit/test_settings.py` 增加求和约束用例；
      `tests/unit/ddl_metadata/jobs/redis/test_job_stores.py` 增加非属主不续期用例。

验证：`uv run pytest tests/unit -q`

## 组二：新增配置项

- [x] 2.1 `src/data_agent/settings.py`：新增 `redis.job_stall_grace_seconds`、
      `redis.max_job_attempts`、`memory.outbox_claim_lease_seconds`、
      `memory.outbox_max_attempts`，均带中文描述与范围约束。
- [x] 2.2 `conf/app_config.yaml`：补齐四个键的显式取值。
- [x] 2.3 确认 `tests/unit/test_settings.py` 的描述完整性用例仍通过。

验证：`uv run pytest tests/unit/test_settings.py -q`

## 组三：记忆索引一致性

- [x] 3.1 `memory/mysql/index_outbox.py`：`claim_outbox` 增加死信过滤与租约推进；
      `retry_outbox` 改数据库端时间并补齐全字段匹配条件；
      `enqueue_rebuild` 改为事务内 `FOR UPDATE` 复核 active 子集。
- [x] 3.2 `memory/indexing/dispatcher.py`：改三段式（短事务领取 → 事务外调用 →
      每项短事务确认/退避），UPSERT 对失效行收敛为 delete，死信计数告警。
- [x] 3.3 测试：新增 `tests/unit/memory/test_index_dispatcher.py`，覆盖事务边界、
      UPSERT 收敛、单项失败隔离；`enqueue_rebuild` 的 active 子集用例。

验证：`uv run pytest tests/unit -q`

## 组四：任务活动索引与停滞巡检

- [x] 4.1 `jobs/redis/keys.py`：新增 `active` 键属性。
- [x] 4.2 `jobs/redis/scripts.py`：`SUBMIT`/`TRANSITION`/`ANSWER` 维护活动索引。
- [x] 4.3 `jobs/redis/state_store.py`：传入新增 KEYS，保持参数协议一致。
- [x] 4.4 `jobs/store.py`：新增 `reap_stalled()` 门面方法与 `_reap_one()` 执行单项
      恢复（复用既有 `transition`/`mark_terminal` 与 outbox 写入）；裁决规则落在
      纯函数 `jobs/recovery.py::stall_action`，活动索引读写落在
      `jobs/redis/activity_store.py`。
- [x] 4.5 `ddl_metadata/worker/maintenance.py`：新增 `reap_stalled_jobs` cron 函数。
- [x] 4.6 `ddl_metadata/worker/settings.py`：注册 cron。
- [x] 4.7 测试：新增停滞巡检单测（pending 重投 / running 回退 / 超限失败 /
      孤儿自愈 / 单项失败不中断）。

验证：`uv run pytest tests/unit -q`

## 收尾

- [x] 全量门禁按 README 执行：`uv lock --check`、`uv run ruff check src tests`、
      `uv run pyright src tests`、`uv run python -m compileall -q src tests`、
      `uv run python -m data_agent.settings`、`uv run pytest -m "not integration"`、
      `docker compose config`、`git diff --check`。仓库基线未纳入 `ruff format`，
      因此只对本任务新增文件做格式化，不改动既有文件的格式。
- [x] 集成测试**未执行**：本机 Docker daemon 未运行，MySQL/Redis 不可用。已改用
      MySQL 方言渲染本次改动的全部 SQL 人工核对语法（timestampadd、行构造器 IN、
      FOR UPDATE SKIP LOCKED、EXISTS/NOT EXISTS 一致性子查询）。
- [x] 更新 spec（新增事务/时钟/死信/锁定复核与 details 约定）、提交、记录 journal。

## 回滚点

四组互不依赖，任一组可单独 `git revert`。组四改动 Lua 协议，回滚需同时回退
`scripts.py`、`state_store.py`、`keys.py` 与 cron 注册。
