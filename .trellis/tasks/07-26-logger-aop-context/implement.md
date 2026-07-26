# 实施计划

1. 重构 `data_agent.logging`，增加 ContextVar 上下文和 Loguru patcher。
2. 增加零参数 AOP 包装器，通过安全反射提取 callable 与参数上下文。
3. 仅在 FastAPI middleware、arq 注册、LangGraph 构图和应用组合层织入 AOP。
4. 将业务日志改为 `logger.<level>(完整消息)`，删除业务层全部日志装饰器、
   `logging_context()` 与 `logger.bind()`。
5. 更新日志规范，明确业务接口与 AOP/formatter 职责。
6. 增加并发隔离、异常识别、入口上下文和业务调用形态测试。
7. 执行日志专项、非集成测试、Ruff、Pyright、编译和配置验证。
