# 简化项目日志并由 AOP 注入上下文

## Goal

业务代码仅调用 logger.<level>(消息)；AOP 统一补充 trace、组件、操作、异常及运行上下文，移除 bind 参数堆叠和 Policy/Classifier/Outcome 设计。

## Requirements

- 业务代码只负责选择日志级别并编写可读消息，例如
  `logger.warning("对话长期记忆提炼延后")`。
- 禁止业务代码通过 `logger.bind()`、`logger.contextualize()` 或日志专用
  Policy / Classifier / Outcome 对象填写上下文字段。
- 禁止在业务类、业务函数和路由处理函数上标注 `@logging_boundary`，也禁止
  业务流程直接调用 `logging_context()`；AOP 必须在组合与框架注册层织入。
- AOP 边界统一补充 `trace_id`、`component`、`operation`、请求或任务标识及
  可安全公开的异常类型。
- 时间、级别、模块、函数、行号、进程号由 Loguru record 与 formatter
  自动提供，不由业务代码传入。
- FastAPI 请求、arq job、LangGraph 节点、SSE 异步生成器和后台维护任务的
  上下文必须隔离，异步并发时不得串写。
- 业务在 `except` 块内只调用 `logger.warning/error(消息)` 时，日志基础设施
  应自动识别当前异常并补充异常类型；异常堆栈只在允许的错误日志中输出。
- 日志基础设施故障不得改变业务返回值、异常传播、Retry、取消或流式响应语义。
- 保留 Loguru，不更换日志框架。

## Acceptance Criteria

- [ ] `src/data_agent` 的业务模块不存在 `logger.bind(...)`。
- [ ] 业务模块不存在 `@logging_boundary` 或直接 `logging_context()` 调用。
- [ ] 业务日志调用只包含级别与消息，不填写结构化日志字段。
- [ ] 同一条业务日志自动包含正确的基础字段和当前 AOP 上下文。
- [ ] 并发请求/任务的 `trace_id`、`component`、`operation` 互不污染。
- [ ] FastAPI、arq、LangGraph、SSE 与后台任务入口均有明确上下文边界。
- [ ] `except` 内的 warning/error 可自动记录安全异常类型。
- [ ] 日志专项测试、非集成测试、Ruff、Pyright 和配置加载全部通过。

## Notes

- 消息内容仍由业务决定，AOP 不替业务选择级别或生成业务文案。
- AOP 只补充可从执行边界、Loguru record 或当前异常自动获得的信息。
