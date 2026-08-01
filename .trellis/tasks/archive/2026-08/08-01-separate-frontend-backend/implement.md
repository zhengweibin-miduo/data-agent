# 前后端分离实施计划

> 本文件仅用于规划；在用户评审并执行 `task.py start` 前不得按此清单实现。

## 阶段 0：评审门禁

- [x] 用户确认 React + TypeScript + Vite。
- [x] 旧 `/`, `/workbench`, `/knowledge`, `/assets` 采用带 `ENABLE_LEGACY_FRONTEND` 的过渡窗口，生产默认关闭。
- [x] API base、部署形态和回滚选择已回写 `prd.md` / `design.md`。

## 阶段 1：契约与后端 API-only

- [x] 盘点并冻结 OpenAPI/JSON/SSE schema：DDL preview、jobs、events、answers、memory、conversation/chat。
- [x] 从 `create_app()` 抽离静态目录和 HTML 路由；保留 CORS、异常投影、API routers、lifespan。
- [x] 增加 API-only 启动测试，确保不依赖前端文件；补充 CORS 允许/拒绝 origin 和 SSE headers 测试。
- [x] 增加兼容开关、弃用日志和显式布尔校验；默认生产关闭。

## 阶段 2：独立前端工程

- [x] 创建 `frontend/` Node 工程、锁文件、`.env.example`、lint/typecheck/build/test scripts。
- [x] 实现类型化 `apiClient` 和统一错误映射，再迁移现有页面能力，组件不直接拼接部署 Origin。
- [x] 实现 workbench：preview → submit 202 → SSE/GET fallback → waiting_input confirmation → terminal summary。
- [x] 实现 knowledge 与 chat；沿用 Semantic Night Canvas、Live Lineage Canvas 和现有无障碍要求。
- [x] 为 API base、SSE 原生重连、状态回退、聊天重试和关键页面流程添加单元/组件测试。

## 阶段 3：部署与切换

- [x] 提供本地开发说明（Vite + Uvicorn + CORS）和同域反向代理配置（静态目录、`/api/`、SSE buffering/timeout）。
- [x] 验证 API base 的空值、`/api` 与绝对 Origin 解析；生产构建不含 secret 或硬编码 localhost。
- [x] 更新 CI：Python lint/pyright/pytest 与前端 lint/typecheck/build/test 均为必需门禁。
- [x] 用组件契约测试验收主流程，并用后端测试验证旧入口默认关闭、显式开关可恢复。

## 阶段 4：质量与收尾

- [x] 运行 `git diff --check`、后端全量非集成测试和前端构建/测试。
- [x] 读取 `code_review.md`，检查 API 契约、SSE 代理边界、配置泄漏和删除范围。
- [x] 使用 `trellis-check` 完成跨层审查，并使用 `trellis-update-spec` 记录独立前端契约。
- [ ] 仅在授权后提交、推送和创建 PR；本任务 base 为 `master`，branch 为 `refactor/separate-frontend-backend-20260801`。

## 风险与回滚点

| 风险 | 预防 | 回滚 |
| --- | --- | --- |
| SSE 被代理缓冲或超时 | 明确响应头、读取超时和 smoke test | 切回同域旧入口/旧代理配置 |
| 前端与 API schema 漂移 | 共享 OpenAPI fixture、契约测试 | 回滚 client adapter，不改任务数据 |
| CORS 配错导致开发不可用 | 环境变量示例与允许/拒绝测试 | 改回同域反代 |
| 删除内嵌入口影响用户 | legacy 开关与迁移说明 | 重新开启 legacy 静态入口 |
| Node 构建链增加维护成本 | 锁文件、CI 缓存、最小依赖 | 保留可部署的上一个静态产物 |
