# 日志上下文盘点

- `logging.py` 已从 Loguru record 获取时间、级别、模块、函数、行号、进程和
  record exception；这些字段无需业务传入。
- 当前业务手填字段集中在 `logger.bind(...)`，包括 trace、component、
  operation、outcome、attempt、revision、stage 和 error_type。
- `create_app()` 当前没有请求上下文 middleware，是 FastAPI AOP 的明确入口。
- `run_ddl_job()` 必须在最外层建立 job 上下文，才能覆盖读取任务失败等早退路径。
- SSE 返回后才实际消费 async generator，必须包装生成器迭代期。
- ContextVar 必须使用 token reset；不能通过全局 `logger.configure(extra=...)`
  动态写请求/任务字段。
