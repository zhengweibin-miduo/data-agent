# 项目结构审查

## 审查范围

本次只读审查覆盖 `src/data_agent/`、`tests/`、`conf/`、`docs/docker/`、`.github/workflows/`、`pyproject.toml` 与 `.trellis/spec/`。结论以当前 `origin/master` 基线创建的任务工作树为准。

## 当前结构

```text
data_agent
├── application.py / main.py       FastAPI 组合根与 ASGI 入口
├── conversation/                  永久会话、轮次与异步记忆提炼
├── ddl_metadata/
│   ├── api/                       HTTP 与 SSE
│   ├── jobs/redis/                Redis 任务状态、事件、租约与 outbox
│   ├── memory/                    记忆领域、用例、MySQL 与索引
│   ├── models/                    DDL、语义与记忆契约
│   ├── persistence/               元数据快照持久化
│   ├── workflow/                  LangGraph 状态、节点、路由与图
│   └── worker/                    arq 执行、维护与生命周期
└── infrastructure/                MySQL、Redis、ES、Qdrant、TEI、LLM
```

API 与 worker 是两个独立进程入口：`src/data_agent/application.py:27-64` 管理 API 资源，`src/data_agent/ddl_metadata/worker/lifecycle.py:36-104` 管理 worker 资源。两处显式装配不是错误重复，而是合理的 composition root。

## 做得好的部分

1. **任务模块是深模块。** `DDLJobStore` 对上提供任务门面，并把 Redis key、codec、Lua、状态、事件、租约和 outbox 隐藏在 `jobs.redis` 内；项目规范也明确这一所有权（`.trellis/spec/backend/directory-structure.md:112-125`）。
2. **工作流拆分清楚。** `state`/`contracts`、`nodes`、`routing`、`graph` 分别拥有状态契约、依赖行为、纯路由和拓扑装配（`.trellis/spec/backend/directory-structure.md:131-136`），当前源码目录与之吻合。
3. **测试结构整体健康。** `tests/unit/` 基本镜像生产包，`tests/integration/` 按 infrastructure、persistence 和跨模块场景组织；pytest 还显式区分 `integration` 与 `tei`（`pyproject.toml:52-60`）。
4. **前端缺失是明确范围，不是结构缺陷。** 当前仓库是 backend-only，前端规范明确禁止凭空引入框架约定（`.trellis/spec/frontend/index.md:7-9,22-36`）。
5. **本机 API 安全边界明确。** `main()` 使用配置中的回环地址（`src/data_agent/main.py:11-18`），FastAPI 统一挂载路由、CORS 和错误映射（`src/data_agent/application.py:109-125`）。

## 发现

### 高：共享记忆被错误归属到 `ddl_metadata`

`conversation` 是根级业务模块，但它的模型、仓储、服务、提炼和 API 都依赖 `data_agent.ddl_metadata`：

- `src/data_agent/conversation/models.py:9-14`
- `src/data_agent/conversation/repository.py:26-27`
- `src/data_agent/conversation/service.py:15-18`
- `src/data_agent/conversation/extraction.py:22-37`
- `src/data_agent/conversation/api.py:18-19`
- `src/data_agent/conversation/mysql_tables.py:18`

这些依赖并非只复用一个稳定接口，而是直接穿透到 `ddl_metadata.memory.mysql.repository`、`ddl_metadata.persistence.schema`、`ddl_metadata.errors` 和具体模型。与此同时，`data_agent_conversation` 这一跨模块身份在 `conversation/service.py:22` 与 `ddl_metadata/memory/application/service.py:43` 重复定义，`conversation/extraction.py:21` 还为常量反向导入 service。

**影响**：通用用户记忆的所有权与目录名不一致；DDL 内部重构会波及会话模块；MySQL adapter、共享 schema、错误和标识符缺少中立 seam；跨模块常量容易漂移。

**建议**：把真正共享的记忆模块整体提升为 `data_agent.memory`，把共享 SQLAlchemy `MetaData`、基础错误和稳定标识符放到中立所有者；`ddl_metadata` 与 `conversation` 都依赖这个根级深模块。应移动真实实现并硬迁移调用方，不增加兼容转发层。

### 中：确定性 domain 直接读取进程全局配置

项目规范明确规定 `ddl_metadata.memory.domain` 只包含确定性转换，不能依赖初始化客户端或技术包（`.trellis/spec/backend/directory-structure.md:115-120,166-169`）。但 `src/data_agent/ddl_metadata/memory/domain/candidates.py:30` 导入全局 `app_config`，并在 `:67-68` 把配置版本写入候选。

**影响**：同一组领域输入的结果还隐含依赖导入时配置；领域测试、历史重放和版本迁移必须操纵进程全局状态，接口没有完整表达行为所需信息。

**建议**：由 application/workflow 在 seam 处传入一个小型不可变版本值（例如 `MemoryProjectionVersions`），domain 只消费显式输入。当前调用点只有 `workflow/nodes.py:455` 与 `memory/application/snapshots.py:31`，改动面可控。

### 中：本机部署边界在 Compose 层失守

API 与 Redis 都绑定回环地址，但 MySQL、Qdrant、Elasticsearch 和 TEI 使用未限定的宿主机端口：

- MySQL：`docs/docker/docker-compose.yml:4-9`，含固定弱口令并暴露 `3306`
- Qdrant：`docs/docker/docker-compose.yml:20-24`
- Elasticsearch：`docs/docker/docker-compose.yml:34-39`，关闭安全功能并暴露 `9200`
- TEI：`docs/docker/docker-compose.yml:49-53`
- 只有 Redis 明确绑定 `127.0.0.1`（`:63-67`）

**影响**：在 Docker Desktop 的常见网络设置下，本地开发数据库和无鉴权检索服务可能暴露给局域网，与“仅面向本机浏览器”的应用边界不一致。

**建议**：所有开发服务端口统一绑定 `127.0.0.1`；若未来需要远程部署，使用独立生产清单、secret 注入、鉴权和网络策略，不复用当前开发 Compose。

### 中：运行入口存在，但项目入口不可发现

README 只有项目简介（`README.md:1-4`）；`pyproject.toml:1-31` 没有 `[project.scripts]`；Compose 只提供依赖服务，没有 API/worker 应用服务。实际入口分别藏在 `python -m data_agent.main`（`src/data_agent/main.py:11-22`）和 arq discovery class（`src/data_agent/ddl_metadata/worker/settings.py:20-64`）。

**影响**：新环境无法从仓库入口判断启动顺序、API/worker 命令、数据库 bootstrap 与可选服务；正确结构存在，但缺少面向使用者的最小接口。

**建议**：至少在 README 记录 `uv sync`、Compose、MySQL bootstrap、API、worker、测试命令；再选择 `[project.scripts]` 或单一跨平台任务入口，避免添加多套脚本。

### 中：质量规范与 CI 对 Compose 的覆盖不一致

`.trellis/spec/backend/quality-guidelines.md:76-90` 把 `docker compose ... config` 列为基线命令，但 `.github/workflows/ci.yml:76-93` 只执行 lock、Ruff、Pyright、compileall、配置加载和 pytest，没有渲染 Compose。

**影响**：Docker 服务定义、端口、卷或 build context 的结构性错误可以绕过 CI，而规范会让维护者误以为已被门禁覆盖。

**建议**：在 CI 增加 `docker compose -f docs/docker/docker-compose.yml config`；可选 ES/Qdrant/TEI 的 live 测试继续保持分层，不必全部塞进主 quality job。

## 未判定为问题

- API 与 worker 各自初始化/关闭资源：它们是独立进程组合根，应保留显式生命周期。
- `ddl_metadata.api.router` 只聚合路由：这是小而稳定的装配模块，不需要为了“加深”而吞并业务逻辑。
- 无前端、无 ORM、无迁移框架：当前规范已明确这些边界。若目标升级为多人或生产部署，再单独评估迁移与前端结构。
- 测试没有 `conftest.py`：仓库明确采用 `tests/helpers` 的可观察检查、factory 与 fake；仅凭 fixture 风格差异不能判定为缺陷。

## 建议顺序

1. 先统一 Compose 回环绑定，风险低且收益直接。
2. 再把共享 memory 所有权从 `ddl_metadata` 提升到根级模块；这是主要结构重构，应独立规划并完整迁移源码、测试和规范。
3. 在该重构中一并消除 domain 对全局配置的依赖和重复 memory source 常量。
4. 补齐 README/运行入口与 Compose CI 门禁。
