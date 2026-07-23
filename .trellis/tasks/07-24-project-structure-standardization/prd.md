# 规范项目结构与文件职责

## Goal

在不改变现有外部行为与存储契约的前提下，识别并修正 `data-agent`
中项目结构、模块归属和文件职责的高价值问题，使代码的依赖方向更清晰、
职责更集中，并降低后续修改和测试时跨目录跳转的成本。

## Background

- 仓库是仅后端的 Python 项目，生产代码位于 `src/data_agent`；不存在前端
  application，本任务不得凭空增加前端目录。
- 项目已经采用 feature-first 结构，并已有后端目录与依赖方向规范；本任务
  应修正实现与规范之间的真实偏差，而不是重新发明一套通用分层。
- 近期代码已完成过 `jobs`、`memory` 等包的职责拆分。热点与 deletion test
  证明 `MemoryRepository`、`MemoryIndexOutboxRepository` 和 `MemoryService`
  已具有真实 depth，本轮不得因文件较大或提交频繁再次拆分。
- `application.py` 与 `ddl_metadata/worker/lifecycle.py` 分别重复承担 Redis、
  MySQL、Elasticsearch、Qdrant、TEI 等资源的初始化和逆序关闭，同时混合各自
  的业务对象装配，是本阶段选定的结构摩擦。
- 配置打包、domain 全局 settings 和测试目录职责错配已有证据，但用户已决定
  将它们保留为后续候选，不纳入第一阶段实施。

## Scope

- 新增一个运行时装配 module，集中 API 与 Worker 共享的资源计划、生命周期
  handle、启动回滚与尽力关闭行为。
- 把 FastAPI lifespan 与 arq startup/shutdown 接入该 module，同时保留各自
  独有的状态发布、索引 setup、LLM、Checkpoint、Graph 和维护动作。
- 更新生命周期测试与后端目录职责规范。

## Requirements

- R1：优先选择能提高 locality、缩小 interface、形成更 deep module 的重构，
  不为单一实现预先创建没有第二个 adapter 或测试需求支撑的抽象。
- R2：明确 HTTP application 与 worker 两个运行入口共享和各自拥有的装配职责，
  避免重复生命周期逻辑及跨上下文硬编码。
- R3：保持现有 HTTP/Pydantic、arq、Redis、MySQL、LangGraph、配置键和结构化
  日志契约；采用 hard migration，不保留退休导入路径的兼容 shim。
- R4：本任务只处理有代码证据和测试收益支撑的结构问题，不引入新业务功能、
  完整 DDD/CQRS、通用 `utils/common/manager/service/repository` 包或推测性抽象。
- R5：用户带入的 `code-documenter` 与 `improve-codebase-architecture` Skill
  保留在任务 worktree 中；前者仅在文档职责或公开对象文档需要调整时使用，
  后者用于候选识别和可视化评审。
- R6：保留已经通过 deletion test 的 deep module。当前 `MemoryRepository`、
  `MemoryIndexOutboxRepository` 与 `MemoryService` 的复杂度删除后会回散到调用方，
  不得仅因文件较大、提交频繁或依赖具体 MySQL implementation 就拆分或增加 port。
- R7：现有 Elasticsearch 与 Qdrant indexing adapter 是已被代码证明的真实
  seam；除非发现可复现的职责泄漏，不重复设计该 seam。
- R8：新的运行时装配 module 必须在启动失败时，按已完成初始化的逆序回滚资源，
  最终向调用方传播原始启动异常；回滚失败不得替换或掩盖原始异常。
- R9：运行时装配采用可扩展资源计划方案，用有序 action/registry 表达资源初始化、
  状态发布、回滚与关闭，并支持 API、Worker 两种运行角色组合不同计划；资源计划
  是 module 的私有 implementation，不允许业务调用方注册或重排 action。
- R10：`start(role, target)` 返回显式 `RuntimeHandle`。API lifespan 局部持有
  handle；Worker 存入内部保留键 `ctx["_runtime_handle"]` 并在 shutdown 取回。
  handle 只记录本次已完成 action 与关闭状态，不暴露具体基础设施客户端；
  `stop(handle)` 必须防止重复关闭，无效或缺失 handle 必须明确报错。
- R11：正常 shutdown 必须逆序尝试关闭 handle 中的全部资源，单个 close 失败
  不得阻止后续资源关闭，且 `logger.complete()` 始终执行。只有一个失败时原样
  抛出该异常；多个失败时抛出保留全部原始异常的 `ExceptionGroup`；完成全部
  尝试后 handle 标记为已关闭。

## Acceptance Criteria

- [ ] API/Worker 的资源装配通过一个外部 interface 完成，共享 implementation
  不再分别维护初始化与关闭顺序。
- [ ] `RuntimeHandle` 只记录已完成 action 与关闭状态，业务调用方看不到具体资源。
- [ ] API 的 `app.state.jobs/memories/conversations` 与 Worker 的
  `ctx["jobs"/"conversation_extractor"/"graph"]` 保持原有可用时机和对象语义。
- [ ] Worker 的索引初始化延后、LLM capability、Checkpoint、dispatch 与 cleanup
  顺序保持现有契约，结构化日志事件及字段保持稳定。
- [ ] 启动中途失败只回滚已成功初始化的资源，按逆序全部尝试，并传播原始启动异常；
  回滚失败仅记录安全结构化日志，不掩盖原始异常。
- [ ] 正常关闭逆序尝试全部资源；单失败原样抛出，多失败抛出 `ExceptionGroup`，
  `logger.complete()` 始终执行，重复关闭被明确拒绝。
- [ ] 最终 `design.md` 明确目标模块、interface、依赖方向、迁移顺序、兼容性和回滚。
- [ ] 最终 `implement.md` 包含按顺序可执行的修改清单、验证命令和风险检查点。
- [ ] 所有修改后的模块通过 Ruff、Pyright、compileall 与相关 pytest。
- [ ] 最终目录规范、实现和测试映射一致，不残留退休路径或无归属文件。

## Out of Scope

- 新增前端 application。
- 新业务功能、HTTP 契约迁移、数据库 schema 迁移或队列协议变更。
- 仅为追求“目录对称”进行的机械搬迁。
- 在没有真实替代实现或测试 seam 需求时批量创建 ports/interfaces。
- 配置加载、domain 全局 settings、测试目录归位及其他未选架构候选。
