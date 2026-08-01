# 前后端分离重构

## Goal

将 Data Agent 从“FastAPI 同时提供 API 与内嵌 HTML/CSS/JS”改造成可独立构建、独立部署的前后端分离结构：FastAPI 只负责业务 API、SSE 与后台资源；前端作为独立工程消费稳定的 HTTP/SSE 契约。首版仍保持本地单用户工具定位，不借此扩大为认证、多租户或公网 SaaS。

## User value

数据工程师可以单独迭代 Schema Loom 工作台的界面与发布节奏，而不必重新打包 Python 服务；后端可以作为无浏览器依赖的 API 服务运行，前端可在本地开发服务器或静态托管环境中访问同一组接口。

## Confirmed facts from repository

- 应用组合根位于 `src/data_agent/application.py:147-183`，当前注册 CORS、API 路由、`/assets` 静态目录和 `/`, `/workbench`, `/knowledge` HTML 路由。
- 当前前端源码位于 `src/data_agent/frontend/index.html`, `app.js`, `styles.css`，通过 FastAPI `StaticFiles` 提供。
- DDL 任务契约已有 `POST /api/v1/metadata/ddl-jobs`（202）、任务查询、可重连 SSE `/events` 和澄清回答 `/answers`；SSE 只暴露公开任务投影，不暴露 LangGraph 内部载荷（`src/data_agent/ddl_metadata/api/jobs.py:24-103`）。
- 已有 DDL preview、知识记忆、会话和聊天 API 路由；前端现有测试验证 `/assets/app.js`、`/assets/styles.css` 和 HTML 入口。
- CORS 来源来自 `conf/app_config.yaml` 的 `api.cors_origins`，当前应用只面向本机浏览器；认证、权限、任务历史列表和完整元数据详情均不存在。
- `pyproject.toml` 当前仅包含 Python/FastAPI 运行依赖，没有 Node 前端工程或构建脚本。
- 归档任务 `07-31-design-frontend-experience` 已确认 Semantic Night Canvas 视觉方向、Live Lineage Canvas 及 API 约束；该设计作为分离后的前端产品基线，而不是重新发明视觉方案。

## Requirements

### Must have

- R1. 新建独立前端工程（推荐 TypeScript + React + Vite，最终实现前可调整），拥有自己的依赖、开发、构建和静态产物目录；不得再从 Python 包路径读取 UI 源码。
- R2. FastAPI 变为 API-only：保留 `/api/v1/**` 业务路由、SSE 响应头和错误投影；移除生产入口对 HTML、`/assets` 和前端源码目录的依赖。
- R3. 前端通过可配置的 API base URL 访问后端，开发环境支持跨源请求，生产环境支持反向代理或静态站点与 API 分域部署；不得把后端地址硬编码进业务组件。
- R4. 明确 CORS、SSE、缓存、错误响应和健康检查的部署边界；SSE 必须保留 `text/event-stream`、`no-cache`、`X-Accel-Buffering: no`，并支持断线后状态查询回退。
- R5. 迁移现有工作台与知识记忆能力：DDL preview、任务受理/状态/SSE、澄清回答、知识搜索/修正/软删除、聊天上下文；不改变现有后端业务语义。
- R6. 前端构建产物可由独立静态服务器托管；部署文档和配置示例说明本地开发、单机生产、容器/反向代理三种边界中的至少两种。
- R7. 删除或迁移现有内嵌前端后，后端测试不再依赖 `/` 或 `/assets`；新增 API 契约测试、CORS/SSE 回归测试和前端构建/类型检查。
- R8. 保留一次可回滚的迁移路径：旧内嵌入口在切换窗口内可通过显式兼容开关保留，或在同一提交中提供清晰的替代启动方式；不得无记录地删除现有用户入口。
- R9. 沿用 Semantic Night Canvas：真实 DDL parser 输出驱动节点与关系，保持键盘可用、状态不只依赖颜色、`prefers-reduced-motion` 和响应式布局。

### Explicitly out of scope

- 登录、角色权限、多租户、公网域名、CDN、团队协作和服务端任务历史。
- 将 Python 业务逻辑复制到前端；前端不得直接访问 Redis、MySQL、LLM 或持有模型密钥。
- 新增 GraphQL、WebSocket 或替换已有 SSE；除非实现阶段发现现有契约无法满足分离部署并另行评审。
- 重新设计产品视觉、增加通用 Dashboard/KPI 页面或自由拖拽画布。

## Acceptance criteria

- [x] 前端工程可在独立目录安装依赖、启动开发服务器并生成静态构建产物；构建不依赖 Python 运行时。
- [x] FastAPI 以 API-only 模式启动时不读取 `src/data_agent/frontend`，`/api/v1` 契约和 OpenAPI 文档可用；旧 HTML 入口由显式兼容开关控制。
- [x] 跨源开发请求通过配置的 CORS origin 成功；生产部署支持同域反向代理或显式 API base URL。
- [x] DDL 任务 202、SSE 原生重连、轮询回退、waiting_input、终态和稳定错误投影在前后端测试中有可观测断言；既有后端 SSE 心跳测试继续通过。
- [x] 前端组件契约覆盖预览 DDL → 提交任务 → 公开阶段 → 澄清确认 → 终态摘要，知识检索路径已迁移。
- [x] CI/本地验证同时覆盖 Python lint/type/test 与前端 lint/type/build/test；文档说明环境变量、端口、代理超时和 SSE 缓冲设置。
- [x] 用户评审规划后才执行 `task.py start`；实现发生在 `in_progress` 阶段。

## Confirmed implementation decisions

- 采用 React + TypeScript + Vite 作为独立前端基线；接受 Node 依赖与独立构建链，以获得类型化 API client、组件化状态管理和独立发布能力。
- FastAPI 生产默认采用 API-only；迁移期保留显式关闭的旧前端兼容开关，完成独立前端与部署验证后再删除旧入口。

## Goal

TBD.

## Requirements

- TBD

## Acceptance Criteria

- [ ] TBD

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
