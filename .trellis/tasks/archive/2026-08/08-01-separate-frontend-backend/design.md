# 前后端分离技术设计

## 1. 目标边界

保留现有 FastAPI 业务能力，拆出独立的 `frontend/` 工程。后端交付 Python API 进程与后台 worker；前端交付静态资源。两者只通过版本化 HTTP JSON 与 SSE 契约通信。

```text
浏览器
  ├─ 静态资源 → Frontend Static Host (Vite build output)
  └─ /api/v1 + SSE → FastAPI API → Redis/MySQL/ES/Qdrant/LLM
                                      ↑
                                  worker/cron
```

## 2. 推荐目录与职责

```text
backend repository
├─ src/data_agent/              # 仅业务、API、资源生命周期
├─ tests/                       # API/领域/基础设施回归
└─ deploy/                      # API/worker/代理配置（如需要）

frontend/                       # 独立 Node 工程（推荐 React + TS + Vite）
├─ src/                         # 页面、组件、API client、SSE adapter、状态
├─ public/                      # 不含后端运行时文件
├─ e2e/                         # 可选的浏览器契约回归
├─ package.json
├─ vite.config.ts
└─ .env.example
```

前端按 `apiClient`、`jobEvents`、`workbench`、`knowledge` 分层；组件只消费类型化 client，不拼接 URL、不处理 Redis/LLM 细节。视觉沿用已确认的 Semantic Night Canvas 与 Live Lineage Canvas。

## 3. API 与 SSE 边界

- API base 由 `VITE_API_BASE_URL`（开发可为空，生产可为 `/api` 或绝对 URL）注入；统一 client 负责 JSON、超时、`error.code/stage/retryable/details` 映射。
- 任务提交仍返回 `202 Accepted`、`job_id`、`status_url`、`events_url`；前端文案必须区分“已受理”和“已开始”。
- SSE adapter 使用 `EventSource`（或带 `Last-Event-ID` 能力的兼容实现），保留 `text/event-stream`、`Cache-Control: no-cache` 和 `X-Accel-Buffering: no`。连接中断后先 GET 权威状态，再决定重连或终态展示。
- `waiting_input` 使用任务返回的 `revision` 与 `question_set_id` 调用 answers；聊天只能起草，用户确认才提交结构化答案。
- preview、memory、conversation/chat API 只经 client 访问；不在前端重新实现 DDL 解析。

后端 `create_app()` 删除 `StaticFiles`、`FileResponse` 和 HTML 路由，保留 CORS middleware、异常映射、API router 和资源 lifespan。若需要平滑迁移，增加显式 `ENABLE_LEGACY_FRONTEND=false` 配置，兼容路由默认关闭并标注废弃期限；API-only 是生产默认。

## 4. 部署拓扑与配置

### 本地开发

Vite `:5173` + Uvicorn `:8000`；`VITE_API_BASE_URL=http://127.0.0.1:8000`，后端 `api.cors_origins` 仅允许开发源。SSE 不经过 Vite mock，直接连接真实 API 或明确的代理转发。

### 单机生产（推荐首个迁移目标）

Nginx/Caddy 提供前端静态目录；`/api/` 反代到 Uvicorn，关闭代理缓冲并设置长于 SSE 心跳的读取超时。浏览器使用同域 `/api`，减少 CORS 复杂度。

### 分域部署

静态站点与 API 使用不同 origin；通过环境变量配置 API origin，后端显式列出允许 origin。禁止把凭据放入前端；当前产品仍是本地单用户无认证。

## 5. 迁移与兼容

1. 先冻结并测试现有 API/SSE 契约，补充 OpenAPI/契约 fixture。
2. 建立独立前端并把当前 `src/data_agent/frontend` 迁移为组件/页面；前端先通过 API base 访问后端。
3. 后端切换 API-only，旧静态目录进入删除或显式 legacy 兼容阶段；更新部署入口、README、CI。
4. 验证前端构建产物、跨源开发、同域反代和 SSE 断线回退后，再移除 legacy 代码（若采用两阶段开关）。

回滚点：前端构建失败时保留旧静态入口；API 契约不可兼容时回滚 client adapter，不回滚数据/任务存储；部署问题通过切回旧容器/静态目录恢复。

## 6. 关键取舍

- React + TypeScript + Vite：类型和生态优先，接受 Node toolchain；比继续扩展内嵌原生 JS 更适合独立发布。
- SSE 保留：现有公开事件模型和 Redis 事件存储已支持可重连；引入 WebSocket 会扩大后端和代理风险。
- 同域反代作为生产默认：最小化 CORS 与 cookie/缓存边界；分域仍由 API base + CORS 支持。
- 不在本任务引入认证：分离部署不等同于安全域拆分，认证需要独立产品决策。

## 7. 非功能要求

前端响应式和可访问性遵循既有设计：键盘主流程、visible focus、`aria-live`、状态文字/图标/形状并用、`prefers-reduced-motion`。后端保持现有日志、超时、Redis 心跳关系和安全错误投影。构建产物不得包含密钥、内部服务地址或调试载荷。
