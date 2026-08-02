# 迁移为 backend/src 与 frontend/src 双根结构

## Goal

把仓库迁移为目录级清晰、构建与测试所有权独立的前后端双根结构：前端源码位于 `frontend/src/`，后端源码直接位于 `backend/src/`。后端不得再保留 `data_agent` 目录层、Python 包命名空间或 `data_agent.*` 导入。

## Background

- 当前重构基线使用根目录 `frontend/` 与 `src/data_agent/`，属于“独立前端 + Python src-layout 后端”，不符合用户要求的 `frontend/`、`backend/` 对称源码所有权。
- `src/data_agent/frontend/` 仍包含迁移期 legacy 前端资源，即使默认关闭，也让后端包继续拥有前端文件。
- 本任务基于已完成的 DDD、Meta Projection、Data Sync、Memory/Conversation 和 Workbench 测试重构继续迁移，不回退这些成果。

## Requirements

- 后端业务源码直接放在 `backend/src/`，例如 `backend/src/memory/`、`backend/src/conversation/`、`backend/src/data_sync/` 与 `backend/src/ddl_metadata/`；不得出现 `backend/src/data_agent/`。
- 删除 `data_agent` Python 命名空间并一次性更新所有源码、测试、脚本、入口点、配置、CI、Docker、文档和当前 Trellis spec 中的 `data_agent.*` 引用；不得增加兼容 shim。
- 删除范围只覆盖源码目录、Python 包命名空间、import、动态模块路径和当前代码规范引用；数据库名/用户、`DATA_AGENT_CONFIG`、日志业务标识、长期记忆 source、现有 CLI 品牌等外部稳定标识保持不变。
- 前端业务源码继续由根目录 `frontend/src/` 唯一拥有，后端不得携带、挂载或打包 legacy 前端资源。
- `pyproject.toml`、`uv.lock`、`conf/` 和全部后端测试必须迁入 `backend/`；后端从 `backend/` 独立完成依赖安装、配置发现、静态检查、测试和包构建。
- `.github/`、`docs/docker/`、根 `README.md` 和仓库级工具配置继续留在根目录，承担跨端 workspace、协作和部署职责；`backend/README.md` 与 `frontend/README.md` 分别承载各自开发说明。
- 保持现有 HTTP/SSE、配置字段、MySQL/Redis/CDC、LangGraph、arq、日志以及前端用户可观察行为，除非规划证据证明某个入口必须随目录迁移而显式改名。
- 不增加数据库、向量索引或历史数据迁移；本任务只迁移仓库和 Python 包结构。
- 保持上一轮建立的渐进式 DDD 与 Ports and Adapters 依赖方向，不用目录移动掩盖新的跨层或跨 bounded-context 依赖。
- 项目 `AGENTS.md` 必须规定任务启动时先由 `trellis-brainstorm` 完成证据勘察和 PRD 收敛，再按是否存在实质性用户决策条件触发 grill-me 对应的 `grilling`；不得机械询问仓库可以回答的事实。

## Out of Scope

- 数据库 schema 迁移、共享开发数据库重置或历史数据清理。
- 新业务功能、HTTP/SSE 契约扩展或前端视觉重设计。
- 为旧 `data_agent.*` 导入提供兼容包、重导出或双路径支持。

## Acceptance Criteria

- [ ] 仓库中不存在 `src/data_agent/`、`backend/src/data_agent/` 或其他后端源码 `data_agent` 目录层。
- [ ] 活动源码、测试、CI、脚本、配置和当前规范中不存在 `data_agent.*` Python 导入；历史归档记录可保留当时准确的路径。
- [ ] `backend/src/` 直接包含后端入口、共享模块和各 bounded context；`backend/tests/` 只测试后端，`frontend/` 独立安装、测试和构建。
- [ ] `backend/pyproject.toml`、`backend/uv.lock` 和 `backend/conf/` 由后端独立拥有，根目录不再承载 Python 包构建或后端运行配置。
- [ ] 后端 wheel/sdist、CLI/FastAPI/arq/Data Sync 入口、配置发现以及 pytest collection 从 `backend/` 新位置正常工作。
- [ ] FastAPI 不再读取或打包任何前端资源；`src/data_agent/frontend/` legacy 路径和对应开关/测试被删除。
- [ ] CI、Docker Compose、开发命令和文档全部使用新路径，并能从规定工作目录重复执行。
- [ ] Ruff、Pyright、compileall、后端 pytest、前端 lint/typecheck/test/build、包构建、Compose config 与 `git diff --check` 获得新鲜验证；外部环境阻断必须如实记录。
- [ ] 迁移后 Memory、Conversation、Data Sync 与 Meta Projection application/domain 内层仍不依赖具体 infrastructure 或外层 adapter。
- [ ] 项目 `AGENTS.md` 已包含 `trellis-brainstorm` 与条件式 `grilling` 的任务启动组合规则，并明确询问条件、跳过条件、一次一个问题和实现停止点。
