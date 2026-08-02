# 规范与代码漂移核验

范围：`AGENTS.md`、`CONTEXT.md`、backend/frontend directory specs 与当前工作树源码、配置、入口、测试。

## 结论摘要

- 规范文本内部清晰地定义了前后端所有权、模块化单体、渐进式 DDD、共享 Memory 边界和测试目录；这些规则可核验，且与大多数当前目录相符。
- Backend directory spec 的“Current Scope/Layout”明显落后于已合并代码：它仍称 backend-only installable app，并遗漏 `chat`、`metadata_indexing`、`memory/versions.py`、`ddl_metadata/jobs/recovery.py` 等已存在包/文件；因此目录表应视为历史快照，不应据此判定代码违规。
- 当前可确认的规则违规/漂移主要是：`metadata_indexing` 已成为真实 bounded context/适配器包，却未被 backend directory spec 的 Scope、Layout、Ownership、Dependency Direction 覆盖；其中直接依赖 `ddl_metadata.persistence.tables`，规范未定义该跨上下文关系。
- `src/data_agent/frontend` 仍有 `index.html/app.js/styles.css`，但 AGENTS 明确允许迁移期只读兼容资源，且应用默认 API-only、显式开关挂载；这是允许的渐进迁移，不是违规。

## 规范基线（可核验）

- AGENTS 将 `frontend/` 定义为前端源码/测试唯一所有者，并将 `src/data_agent/frontend/` 限定为显式开关下的只读迁移资源；后端源码唯一所有者为 `src/data_agent/`（`AGENTS.md:68-75`）。
- 后端规则要求按 bounded context 组织、domain/application/adapters/infrastructure 分层，并明确“现有模块按实际改动渐进迁移，不要求一次性重排”（`AGENTS.md:77`）。
- 前端规则要求轻量 feature-first：应用壳、feature、共享 API，各自状态所有权清晰（`AGENTS.md:79`）。
- backend spec 的 Layout/Scope 仍写“backend-only installable Python application”，并列出旧式完整树（`spec/backend/directory-structure.md:3-11,15-117`）；同时声称测试镜像 `tests/unit/`、集成测试位于 `tests/integration/`（`...:119-122`）。
- frontend spec 列出 `frontend/src/api`, `knowledge`, `workbench` 与 `src/data_agent/frontend` migration-only，并要求所有 URL 经 API/SSE adapter（`spec/frontend/directory-structure.md:4-20,26-34`）。

## 实际结构与 bounded contexts

实际 `src/data_agent` 包包含：`answer_readiness`、`chat`、`conversation`、`data_sync`、`ddl_metadata`、`memory`、`metadata_indexing`、`infrastructure`、`models`、`persistence` 等；其中 `chat`、`metadata_indexing` 在 backend spec 的 Layout/Ownership/Dependency 中完全缺失（由实际 `find src/data_agent -maxdepth 3 -type f` 核验）。

- `application.py` 是组合根，装配 chat、conversation、DDL、memory 与基础设施（`src/data_agent/application.py:13-38,69-79`），符合 AGENTS 的“应用启动位置选择适配器/基础设施”要求。
- `memory` 已按 `domain`、`application`、`indexing`、`mysql` 分层，目录与规范中 Memory 设计一致；workflow 通过 `memory.domain`/`memory.mysql` 使用它，而未发现 `memory -> ddl_metadata` 反向导入。
- `conversation` 依赖 root memory application/mysql（例如 `src/data_agent/conversation/service.py:19-20`），未导入 `ddl_metadata`，符合 shared-memory boundary（规范 `...:208-213,259-264`）。
- `ddl_metadata` 仍持有 transport、job、persistence、workflow、worker，且 API memory 路由依赖 `memory.application.service`（`src/data_agent/ddl_metadata/api/memories.py:5`）；这与规范声明 DDL 仅保留编排/快照并复用 root memory 的方向一致。
- `chat` 是独立 HTTP/服务入口：`application.py` 注册 `chat_router` 并构造 `ChatService`（`application.py:15-16,75-79`）；backend spec 未记录这一 bounded context，属于规范落后而非目录本身违规。

## 明确的代码—规则漂移

### 1. `metadata_indexing` 未在规范中建模（规则不完整）

当前代码存在完整的 `src/data_agent/metadata_indexing/`（desired/dispatcher/elasticsearch/models/projections/qdrant/rebuilder/repository/search/tables/value_refresh），但 backend spec 的 Scope、Layout、Ownership、Dependency 均没有该包（`spec/backend/directory-structure.md:3-11,15-117,124-177,179-213`）。它不是表面目录命名：

- `metadata_indexing/desired.py:8`、`projections.py:14`、`value_refresh.py:36` 直接导入 `data_agent.ddl_metadata.persistence.tables`。
- 这形成 `metadata_indexing -> ddl_metadata` 的跨 bounded-context/技术包依赖；现有规则只明确禁止 `conversation -> ddl_metadata`、`memory -> ddl_metadata`（`spec/backend/directory-structure.md:208-213,259-264,291-293`），没有说明 metadata-indexing 是否属于 DDL 子模块、共享 kernel、还是应通过端口/契约协作。

判定：不是可以仅靠命名解释的表象；属于规范未覆盖的真实依赖边界，规则不清晰。需要主代理决定是把 `metadata_indexing` 纳入 DDL bounded context，还是补充独立 context/port 规则。

### 2. backend “Current Scope/Layout” 已过时

代码已具备前端、chat、memory、metadata indexing，但 spec 仍称 backend-only（`spec/backend/directory-structure.md:3-11`）。AGENTS 与 frontend spec 已承认独立 `frontend/` 应用（`AGENTS.md:68-75`; `spec/frontend/directory-structure.md:4-22`），因此该表述与已合并代码冲突。应更新 spec，不应移除代码。

遗漏的已存在路径包括：

- `src/data_agent/chat/*`；
- `src/data_agent/metadata_indexing/*`；
- `src/data_agent/memory/versions.py`；
- `src/data_agent/ddl_metadata/jobs/recovery.py`；
- `src/data_agent/data_sync/locks.py`；
- `src/data_agent/frontend/*` 兼容资源。

这些属于目录清单漂移，不能单独证明架构违规。

## 前后端所有权与迁移状态

- 实际前端源码集中在 `frontend/src`：`api`、`knowledge`、`workbench`、`App.tsx`、`main.tsx`、`styles.css` 均与 frontend spec Layout 相符；测试随 feature 放在 `frontend/src/**/*.test.*`，另有 root `tests/unit/test_frontend.py`。
- root frontend API 使用 `frontend/src/api/client.ts`、`dataAgent.ts`、`jobEvents.ts`；未发现从 `frontend` 导入 Python `src/data_agent` 的路径（规范要求见 `AGENTS.md:74-75`）。
- `src/data_agent/frontend/index.html`, `app.js`, `styles.css` 实际存在，但 `tests/unit/test_frontend.py` 验证默认不挂载、仅 `ENABLE_LEGACY_FRONTEND=true` 时提供旧入口（`tests/unit/test_frontend.py:42-80`）。这与 AGENTS 及 frontend spec 的 migration-only 约束（`AGENTS.md:70-71`; `spec/frontend/directory-structure.md:18-24`）一致，属于允许的渐进迁移。

## 测试 seam 与规范覆盖

- backend spec 要求 unit 镜像包边界、integration 场景化（`spec/backend/directory-structure.md:119-122`）；实际 `tests/unit/{answer_readiness,chat,conversation,data_sync,ddl_metadata,infrastructure,memory,metadata_indexing}` 与 `tests/integration/...` 均按包/场景组织，符合该 seam 规则。
- `tests/unit/test_frontend.py` 以 `create_app()` + `httpx.ASGITransport` 验证 API-only、legacy switch、CORS 等公共 HTTP seam（`tests/unit/test_frontend.py:42-117`），而非 mock 内部协作者，符合 AGENTS 测试要求（`AGENTS.md:37-41`）。
- frontend feature tests 与 API tests 实际位于 `frontend/src/knowledge`, `frontend/src/workbench`, `frontend/src/api`，和 feature-first seam 一致；frontend quality spec 要求 npm lint/typecheck/test/build 及 root gates（`spec/frontend/quality-guidelines.md:3-17`）。
- backend spec 的 Shared Memory 场景要求 schema identity、无 retired imports、Ruff/Pyright/compileall/pytest（`spec/backend/directory-structure.md:286-295`）；当前测试树已有 memory/persistence tests，但未在本次只读核验中执行命令，故不能声称门禁通过。

## 允许的渐进迁移 vs 真违规

**允许/不应误报：**

- `src/data_agent/frontend` 兼容静态资源：AGENTS 明确允许，只读、显式开关；已有测试覆盖。
- 现有非严格 DDD 文件（如 `conversation` 仍有 `api.py`, `repository.py`, `service.py` 直列）：AGENTS 规定渐进迁移，不要求一次性创建空 domain/application/adapters 层（`AGENTS.md:77`）。
- backend spec 目录清单遗漏文件：属于规范滞后/表象，不能据此删除或重排代码。

**需要规范或代码边界决策的真实问题：**

- `metadata_indexing` 的 bounded context 归属及其对 `ddl_metadata.persistence.tables` 的直接依赖未定义；这是当前最明确的规则缺口。
- backend spec “backend-only” 与独立 frontend 现实冲突，应更新为当前全仓库结构。

## 规则清晰度结论

前后端所有权、API-only/legacy 开关、memory 与 conversation 的反向依赖禁止、测试目录及渐进迁移原则均清晰且有可执行证据。bounded context 列表不完整：`chat` 与 `metadata_indexing` 未被 backend directory spec 命名；尤其 `metadata_indexing -> ddl_metadata.persistence` 依赖没有端口/共享内核/归属规则，导致该处无法仅依据现行规范判断合规。建议先补规范中的 context 清单与依赖矩阵，再决定是否需要代码迁移。
