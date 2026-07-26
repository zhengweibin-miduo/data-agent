# 清理 DDL 任务契约与激活时延

## Goal

清理审查记录的四个 DDL 任务层遗留问题：公开投影泄漏内部字段、回答受理后仍返回
上一轮问题、尝试次数读改写不原子、以及受理后白等一个调度周期。

## Background

本任务基于 `fix/arch-review-reliability-20260726`（停滞巡检与一致性修复），因为
四项改动都落在该分支已经改过的 Lua 协议与任务门面文件上，独立开分支只会制造冲突。

- `JobRecord.graph_version` 是 worker 判断"任务能否由当前图版本继续解释"的内部
  兼容字段，调用方无从使用，却出现在公开投影里。
- 回答受理后 `questions_json` 未被清除，`GET /ddl-jobs/{id}` 在 `pending` 状态下
  仍返回上一轮问题；而事件投影只在 `waiting_input` 携带问题，同一状态两个读路径
  语义不一致。
- `mark_running` 先读 `attempt` 再写 `attempt + 1`，读改写之间若有其他推进者插入，
  计数会被覆盖为陈旧值。当前靠"同一任务只有一个活动 actor"的隐式前提成立，
  但没有任何机制强制该前提。
- `submit` 与 `submit_answers` 只写 dispatch outbox，激活要等 `dispatch_pending`
  cron（每 10 秒）才被 worker 看到，平均 5 秒、最坏 10 秒。叠加在交互式问答流程上
  体感明显。

## Requirements

- 公开投影不得包含 `graph_version`；worker 通过内部读取路径获取该字段。
- 回答受理后不得再返回上一轮问题列表；但重复提交的幂等判定必须继续成立。
- 尝试次数必须在状态转换脚本内原子递增，调用方不得先读后写。
- 受理路径应在写入 outbox 之后立即调度该激活；outbox 仍是唯一可恢复的调度请求，
  立即调度失败只能退回周期调度，不得撤销已成立的受理承诺。
- API 启动不得因为立即调度能力而变成"Redis 必须在启动瞬间可达"。

## Constraints

- 不改变 arq 重试预算、退避算法与既有状态转换语义。
- Lua 协议的既有返回码语义保持不变。

## Acceptance Criteria

- [x] `JobRecord` 不再包含 `graph_version`，worker 仍能正确拒绝图版本不匹配的任务。
- [x] 受理回答后读取不再返回上一轮 `questions`，且同一回答重复提交仍判定为幂等。
- [x] 转换脚本用 `HINCRBY` 递增尝试次数，`mark_running` 不再预读权威记录。
- [x] 注入队列时受理后立即调度激活；入队失败仍报告受理成功。
- [x] 生命周期不发起启动期连接，无 Redis 环境下单元测试仍可运行且不产生重试等待。
- [x] 上述行为均有单元测试覆盖。
- [x] README 记录的基础质量门禁全部通过。
