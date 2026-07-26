# 统一 worker 的可重试错误判定

## Goal

让 `DataAgentError.retryable` 成为可重试性的权威来源，消除"项目自有错误契约"与
"worker 内置基础设施异常清单"两套判定并存且互不相通的问题。

## Background

架构审查问题 P1-18。`src/data_agent/ddl_metadata/worker/job_runner.py` 用模块级
`_RETRYABLE` 元组（直接 import `openai`、`sqlalchemy`、`redis` 的异常类型）判定
是否重试：

- `DataAgentError` 定义了 `retryable` 字段，但 job_runner **从不读取它**。因此
  一个明确声明 `DataAgentError(retryable=True)` 的业务/基础设施错误会被直接判为
  永久失败，声明形同虚设。
- 终态投影把 `retryable` 硬编码为 `False`，即使底层确实是瞬态错误也如此。
  而 `JobError.retryable` 的字段说明是"错误是否可重试"——描述错误本身的性质，
  不是"本次是否还会重试"，硬编码与声明的契约不一致。
- worker 编排层因此必须知道每个第三方库的异常分类，新增或更换一个依赖就要改
  job_runner。

## Requirements

- 可重试判定必须优先读取 `DataAgentError.retryable`；只有非 `DataAgentError`
  的第三方异常才回退到内置瞬态异常清单。
- 终态错误投影的 `retryable` 必须反映底层错误本身是否瞬态，而不是恒为 `False`。
- 保留内置瞬态异常清单作为第三方异常的回退，并在代码中写明后续新增基础设施
  应在边界处包装为 `DataAgentError(retryable=True)`，而不是继续扩充该清单。
- 不改变 arq 重试预算、退避算法与既有状态转换语义。

## Constraints

- 只改动 `job_runner.py`，避免与其它审查修复分支产生文件冲突。
- 不改变 `JobError` 与 `JobRecord` 的字段结构。
- 累计激活上限的配置化（`redis.max_job_attempts`）属于停滞巡检任务
  `07-26-arch-review-reliability` 的范围，本任务保持现有字面量不动。

## Acceptance Criteria

- [x] `DataAgentError(retryable=True)` 在预算内会被重试，而不是直接判为失败。
- [x] `DataAgentError(retryable=False)` 不会被重试，即使其类型恰好属于内置清单。
- [x] 非 `DataAgentError` 的瞬态第三方异常仍按内置清单重试。
- [x] 终态投影的 `retryable` 与底层错误的瞬态性一致。
- [x] 上述判定有单元测试覆盖。
- [x] README 记录的基础质量门禁全部通过。
