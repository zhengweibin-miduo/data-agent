# 修复架构审查发现的可靠性与一致性缺陷

## Goal

修复项目架构审查中确认的高严重度缺陷，消除三类可观察故障：DDL 任务在重试预算
耗尽后永久卡死、记忆派生索引与 MySQL 权威状态发散（含已删除内容残留）、以及
内部错误消息穿透到公开 HTTP 契约。

本任务只做定位明确、触发路径已被证据支持的修复，不做全局分层重构。

## Background

架构审查记录的相关问题编号（用于追溯）：

- P0-1：arq `max_tries` 被基础设施 `Retry` 消耗后，任务停留在 `pending`/`running`
  且无任何组件重新激活。
- P0-2：记忆索引 dispatcher 在单个 MySQL 事务内持行锁执行 ES/Qdrant/TEI 网络调用。
- P0-3：`enqueue_rebuild` 可把并发删除产生的 DELETE 期望覆写为 UPSERT；dispatcher
  对失效行执行 UPSERT 或空确认，导致已删除内容在派生索引永久残留。
- P0-4：`source_lease_seconds` 校验取 max 而非和，等待期租约可先于回答截止过期；
  ANSWER 脚本无条件 `EXPIRE` 租约键，不校验属主。
- P0-5：`DataAgentError.details` 默认回填内部 `message`，经 API 与事件流外泄。
- P1-12：outbox 退避时间用 Python UTC-naive 时间写入，却与数据库 `now()` 比较。
- P1-13：两个 outbox 无尝试上限，确定性失败会无限重试。
- P1-20：`expire_waiting` 循环无逐项错误隔离，一个孤儿成员阻断整轮处理。

## Requirements

### 任务可靠性

- 非终态 DDL 任务必须被一个权威索引跟踪，使维护任务能在不扫描 Redis 键空间的
  前提下发现停滞任务。
- 停滞判定阈值必须严格大于 arq 自身的任务超时，确保被巡检重新激活的任务不会与
  仍在执行的 arq 任务并发。
- 巡检对 `pending` 任务重新投递激活请求；对 `running` 任务先原子回退为 `pending`
  再投递；超过尝试上限的任务转入 `failed` 终态并携带稳定错误码。
- 巡检必须对孤儿索引成员（Hash 已过保留期）自愈清理，不得因单项失败中断整轮。
- `expire_waiting` 必须逐项隔离错误，单个成员失败不影响同轮其余到期任务。

### 来源租约

- 配置校验必须要求 `source_lease_seconds >= worker_job_timeout_seconds +
  waiting_timeout_seconds`，覆盖"执行后进入等待"的串联占用窗口。
- ANSWER 脚本续期来源租约前必须校验租约属主，不得延长其他持有者的租约。

### 记忆索引一致性

- dispatcher 必须以短事务领取、事务外执行外部调用、短事务确认的三段式运行，
  MySQL 行锁不得跨越任何外部网络调用。
- 领取必须写入租约（推进 `available_at`），使并发 dispatcher 不重复处理同一行，
  且进程崩溃后该行能在租约到期后自动重回可领取状态。
- UPSERT 语义必须是"收敛到权威状态"：权威行缺失或非 `active` 时执行删除，
  不得跳过确认或写入失效内容。
- `enqueue_rebuild` 只能为重建事务内实际锁定且仍为 `active` 的行生成 UPSERT 期望，
  不得覆盖并发提交的 DELETE 期望。
- 确认与退避都必须按完整期望条件匹配，迟到的 worker 不得影响后来覆盖的新期望。

### 时间与重试预算

- outbox 退避时间必须由数据库端生成，与领取条件使用同一时钟。
- 记忆索引 outbox 必须有尝试上限；超限行保留在表中但不再被领取，并产生可观测日志。

### 错误契约

- `DataAgentError.details` 默认为空，内部 `message` 只进日志，不进 HTTP 响应与事件流。

## Constraints

- 不改变现有公开 HTTP 契约的字段语义（`JobRecord`、错误响应结构保持兼容）。
- 不引入新的外部依赖。
- 新增配置项必须在 `conf/app_config.yaml` 显式给值，并带中文字段描述。
- 不做本次审查中记录的分层重构（全局单例改注入、配置惰性加载、worker 组合根上移、
  memory 门面收口），这些属于独立任务。
- 现有 Redis Lua 协议的返回码语义保持兼容；新增键不得破坏既有键空间约定。
- ~~不新增数据库列~~：项目确认尚未部署、无需考虑升级迁移后，该约束已由用户显式
  解除。新增列与索引由 `docs/docker/mysql/data_agent.sql` 建表，已有本地卷需要
  重建后才会生效。

## Acceptance Criteria

- [x] 非终态任务进入活动索引，终态转换将其移除；孤儿成员被巡检自愈清理。
- [x] 停滞的 `pending` 与 `running` 任务能被巡检重新激活，超过尝试上限时转入
      `failed` 并带稳定错误码，且有单元测试覆盖三条分支。
- [x] `expire_waiting` 在单项抛错时仍处理同轮其余成员。
- [x] 配置校验拒绝 `source_lease_seconds < worker_job_timeout + waiting_timeout`。
- [x] ANSWER 脚本只在租约属主匹配时续期，有测试覆盖非属主场景。
- [x] dispatcher 的外部调用全部发生在 MySQL 事务之外，有单元测试断言事务边界。
- [x] 权威行缺失或非 active 时，UPSERT 期望被收敛为派生索引删除。
- [x] 重建只为事务内锁定且仍为 active 的行生成 UPSERT 期望。
- [x] outbox 退避时间由数据库端生成；超过尝试上限的行不再被领取。
- [x] `DataAgentError` 默认不再把内部 message 放入 `details`。
- [x] 确认阶段复核权威一致性，释放行锁引入的丢失更新窗口被关闭。
- [ ] README 记录的基础质量门禁全部通过：`uv lock --check`、
      `uv run ruff check src tests`、`uv run pyright src tests`、
      `uv run python -m compileall -q src tests`、
      `uv run python -m data_agent.settings`、`uv run pytest -m "not integration"`、
      `docker compose -f docs/docker/docker-compose.yml config`、`git diff --check`。
      仓库基线未纳入 `ruff format`，本任务同样不对既有文件做格式化改动。
