# 拆分 store 模块职责

## Goal

将 `src/data_agent/ddl_metadata/jobs/store.py` 中聚集的 Redis 任务存储职责拆分到边界清晰的模块，降低单文件复杂度，同时保持现有任务生命周期、原子性和外部行为不变。

## Background

- 当前 `store.py` 同时拥有 Lua 原子脚本、Redis 键空间、公开任务投影、状态转换、回答恢复、来源租约、dispatch outbox、等待超时和 checkpoint cleanup outbox。
- `DDLJobStore` 被 API、worker、memory service、应用组合和集成测试直接使用；`question_set_id` 还被 workflow 和测试直接导入。
- Redis job Hash、来源租约、dispatch/waiting/cleanup 有序集合之间存在必须保持的原子契约。
- 当前工作树干净，分支为 `feature/langgraph-ddl-metadata-20260717`。

## Requirements

- R1：按稳定职责边界拆分 `store.py`，不得仅为了缩短文件而机械搬移代码。
- R2：保持任务提交、状态转换、回答提交、来源租约、等待超时、dispatch 和 checkpoint cleanup 的现有 Redis 原子语义。
- R3：保持 `DDLMetadataError` 的错误码、阶段、HTTP 状态和安全信息不变。
- R4：保持现有 Redis key 格式、Hash 字段、TTL、outbox member、arq job ID 和公开 `JobRecord` 投影格式不变。
- R5：避免让 API 路由、worker 或 memory service 直接操作 Redis 内部字段。
- R6：为拆分出的确定性逻辑补充或调整无需 live Redis 的单元测试；保留现有 Redis/MySQL 集成覆盖。
- R7：新模块和公开对象遵循中文 Google Style Docstring、类型标注和 side-effect-free `__init__.py` 约束。
- R8：保留 `DDLJobStore` 作为对外兼容门面，由门面组合内部专职组件；现有生产调用方无需分别装配多个 store，但导入路径统一迁移到 `jobs.ddl_job_store`。
- R9：拆分出的职责模块文件名使用完整 `snake_case` 并统一以 `_store.py` 结尾，对应专职类使用 `PascalCase` 并统一以 `Store` 结尾。

## Acceptance Criteria

- [x] AC1：`jobs` 包中的文件名和对象名能够表达各自职责，原 `store.py` 不再同时内嵌所有 Lua、编解码、租约和 outbox 实现。
- [x] AC2：所有现有生产调用方仍通过明确的 jobs 层接口完成操作，不绕过状态机和原子脚本。
- [x] AC3：任务提交及回答提交的多键写入仍通过单次 Lua 原子执行完成。
- [x] AC4：终态转换仍原子释放来源租约、清除敏感字段、设置 TTL 并登记 checkpoint cleanup。
- [x] AC5：重复激活、重复回答、过期回答、非法状态转换和来源冲突的行为与现有实现一致。
- [x] AC6：Ruff、Pyright、compileall、非集成单测以及 Redis 相关集成测试通过；若 live 服务不可用则明确报告。
- [x] AC7：相关 backend spec 的目录结构和 Redis job store 契约同步到新模块边界。
- [x] AC8：jobs store 模块均使用表达完整职责的 `snake_case` 名称并以 `_store.py` 结尾，专职类使用 `PascalCase` 并以 `Store` 结尾。

## Out of Scope

- 不改变公开 HTTP API、Pydantic 请求/响应模型或业务状态枚举。
- 不改变 Redis 数据结构、配置项、保留期限或 arq/LangGraph 工作流协议。
- 不引入新的外部依赖，不迁移到其他存储系统，不拆分微服务。
