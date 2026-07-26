# 日志 AOP 上下文设计

## 设计边界

业务代码保留最直接的日志接口：

```python
logger.warning("对话长期记忆提炼延后")
```

业务消息与级别属于业务判断。`trace_id`、`component`、`operation`、执行入口
标识和异常元数据属于日志基础设施，由 AOP 注入。

## 核心结构

在 `data_agent.logging` 中维护一个不可变的 `ContextVar` 日志上下文，并提供：

- `logging_context(**context)`：仅供日志基础设施内部使用，进入执行边界时
  合并上下文，退出时按 token 恢复，保证嵌套和并发隔离。
- Loguru patcher：每条 record 创建时合并当前上下文、Loguru 自动字段和当前
  `except` 异常类型。patcher 失败时保留原始日志。
- formatter：文本输出直接使用 record 字段；JSON 只序列化固定白名单，拒绝
  任意业务载荷。

不在业务模块暴露 `bind` helper、Policy、Classifier、日志 Outcome、
`logging_context()` 或 `@logging_boundary`。

`logging_boundary()` 的 `component`、`operation` 和 `context_factory` 均为可选。
默认从 callable 的 module、qualified name、签名绑定参数，以及参数中允许的
Mapping/Pydantic/dataclass 字段反射提取。反射严格使用字段白名单，不遍历任意
对象、不读取 property、不检查调用栈或局部变量。

## AOP 接入点

- FastAPI：应用 middleware 为单次请求建立 trace 上下文；component 和
  operation 由 Loguru record 的模块与函数自动派生。
- arq：只在 `WorkerSettings` 注册函数、cron 和生命周期 hook 时包装 callable。
- LangGraph：只在 `graph.add_node()` 的构图 seam 包装节点 callable；节点实现
  不标注日志装饰器。
- SSE：包装 async generator 的实际迭代期，而不是只包装返回
  `StreamingResponse` 的路由函数。
- 后台维护、长期记忆索引和对话提炼：在 worker/composition root 注册时包装，
  运行期间继承统一上下文。

## 异常与安全

patcher 使用当前 `sys.exc_info()` 识别 `except` 块中的异常，只记录异常类名；
ERROR/CRITICAL 且 Loguru record 已携带 exception 时才输出清洗后的堆栈。消息、
SQL、DDL、凭据、完整请求体和用户内容不进入自动上下文。

包装器必须保留函数签名、返回值、生成器行为以及 `CancelledError`、
`GeneratorExit`、`arq.Retry` 和业务异常的原始传播语义。

## 放弃的方案

- 每条日志 `logger.bind(...)`：业务流程重复填写基础设施字段。
- 每个场景维护 Policy / Classifier / Outcome：只是把参数填写搬到另一层。
- 全局 `logger.configure(extra=...)` 动态更新：并发任务会共享并污染上下文。
