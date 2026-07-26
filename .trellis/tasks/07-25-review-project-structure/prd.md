# 审查项目结构

## Goal

完成项目结构审查并修复确认的问题，使共享模型、长期记忆、会话、DDL 元数据和本地运行基础设施拥有清晰所有权，同时保持现有外部行为兼容。

## Confirmed Facts

- 用户已确认实施审查报告中的修复。
- 审查对象为当前任务工作树中的完整仓库。
- 结论必须来自真实文件、符号和配置，不使用虚构案例或通用架构套话。
- 当前仓库是 backend-only，本次不新增前端。
- API 与 worker 是独立进程组合根，保留各自显式资源生命周期。

## Requirements

- 将跨功能共享的契约从 `data_agent.ddl_metadata.models` 硬迁移到根级 `data_agent.models`。
- 将长期记忆实现从 `data_agent.ddl_metadata.memory` 硬迁移到根级 `data_agent.memory`，不保留兼容转发模块。
- 将会话与记忆共同使用的错误、标识符和 SQLAlchemy `MetaData` 提升到根级所有者，消除 `conversation -> ddl_metadata` 依赖。
- 让 memory domain 通过显式不可变值接收 `content_version` 和 `projection_version`，不得导入全局 settings。
- 保持 HTTP 路径/状态码/响应字段、Pydantic 契约、数据库 schema/table、Redis key/Lua、arq 函数和 cron 名称、LangGraph 状态与配置键不变。
- 将 MySQL、Qdrant、Elasticsearch、TEI 的本地 Compose 宿主端口绑定到 `127.0.0.1`。
- 在 README 中提供安装、依赖服务、数据库初始化、API、worker 与验证入口；只为 API 增加可靠的项目脚本，worker 继续使用 arq 官方发现路径。
- 在 CI 增加 Docker Compose 配置渲染门禁。
- 更新生产代码、测试、README 和当前 Trellis 规范中的全部活动路径，归档任务历史不做迁移。

## Acceptance Criteria

- [x] `src/data_agent/conversation/` 不再导入 `data_agent.ddl_metadata`。
- [x] 活动源码、测试和当前规范中不再引用旧 `data_agent.ddl_metadata.models`、`data_agent.ddl_metadata.memory`、`data_agent.ddl_metadata.errors` 或 `data_agent.ddl_metadata.identifiers` 路径。
- [x] 所有 SQLAlchemy tables 仍共享同一个根级 `MetaData` 实例，现有 schema/table 名和 DDL 保持不变。
- [x] memory domain 不导入 `data_agent.settings`，版本字段由 application/workflow 显式传入；改变版本不改变 memory UID 或 content hash。
- [x] Compose 中所有宿主机服务端口均绑定 `127.0.0.1`，且配置可成功渲染。
- [x] README 可从空白环境指导启动依赖、初始化 MySQL、运行 API/worker 和执行验证。
- [x] CI 执行 Compose 配置渲染。
- [x] Ruff、Pyright、compileall、配置加载、非集成测试、Compose 渲染和 `git diff --check` 通过。
- [x] 已尝试执行 MySQL/Redis 集成测试；外部端点重置连接且 Docker Desktop daemon 不可用，限制已如实记录。

## Out of Scope

- 不执行提交、推送或创建 Pull Request。
- 不对运行时性能、安全漏洞或业务逻辑正确性做全面专项审计；仅在它们直接暴露结构问题时提及。
- 不新增前端、ORM、数据库迁移框架、生产部署清单或新的基础设施 adapter。
- 不为旧 Python 模块路径保留兼容 shim。

## Evidence

- 审查结论：`research/project-structure-review.md`
- 基线验证：`uv lock --check`、Ruff、Pyright、非集成 pytest。
- 修复后验证：`60 passed, 21 deselected`；Compose 配置渲染成功且所有宿主端口为 `127.0.0.1`。
- 环境限制：集成测试 `19 failed, 1 passed, 61 deselected`，失败来自本机 MySQL/Redis 连接重置。
