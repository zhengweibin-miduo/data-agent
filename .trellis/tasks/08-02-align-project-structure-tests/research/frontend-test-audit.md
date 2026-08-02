# 前端测试臃肿审计（只读取证）

## 规模与集中度

- `frontend/src/workbench/WorkbenchPage.test.tsx` 706 行、34 个测试，是最大前端测试文件。
- `frontend/src/api/dataAgent.test.ts` 460 行、11 个测试；`App.test.tsx` 223 行、12 个测试；`api/jobEvents.test.ts` 179 行、7 个测试；`api/client.test.ts` 83 行、6 个测试；`knowledge/KnowledgePage.test.tsx` 70 行、3 个测试。
- 前端共约 73 个测试、1,721 行测试代码；Workbench 单文件覆盖 chat retry、deep-link/sessionStorage 恢复、提交幂等、DDL 预览、SSE、澄清回答等多个 interface。

## 重复 setup 与 fixture 候选

- `WorkbenchPage.test.tsx:32-43` 每次清理 `localStorage`、`sessionStorage`、history 并重置 6 个 API/SSE mock；`App.test.tsx:24-36` 重置 history、浏览器对话框和 8 个 mock；`KnowledgePage.test.tsx:12-19` 再次设置 history 与 memory API 默认值。
- `WorkbenchPage.test.tsx:24-29` 定义 `waitingJob`，`jobEvents.test.ts:21-25` 又维护单独 literal。预览结果、提交响应及“填入 DDL → 点击提交”链在 Workbench 多个场景重复。
- 可提取 `resetBrowserState`、feature 级默认 API fake、`renderWorkbench`、`fillAndSubmitDDL`、`waitingJob`/preview/accepted-submission factory；默认值应按 feature 分层，避免一个全局 fixture 变得更宽。

## implementation-coupled 测试证据

- Workbench 测试直接 mock `../api/dataAgent` 与 `../api/jobEvents`，并从 `connectJobEvents.mock.calls[0][1]` 取出 `onEvent`、`onJob`、`getAuthoritativeJob` 回调驱动组件。这验证了当前回调组装方式，而不仅是页面可观察行为。
- 多个测试直接构造并断言 `schema-loom-pending-submission` 的 sessionStorage payload、history 内部路径和 exact call count。存储 key/格式属于明确恢复契约时应保留在 adapter/interface 测试；页面测试不应重复验证其实现细节。
- `jobEvents.test.ts` 的 `FakeEventSource` 通过静态 `latest`/listeners 模拟浏览器对象；其本质是系统边界 adapter，可移入共享 test helper，但事件重连语义仍应在 adapter seam 保留。

## 重复覆盖候选

- SSE authoritative refresh、native reconnect 与 polling fallback 同时在 `jobEvents.test.ts` 和 Workbench 页面测试出现。建议 adapter 测试完整证明 SSE/reconnect/polling 状态机；页面层只保留“收到 authoritative job 后呈现/启用行为”的 tracer case。
- `dataAgent.test.ts` 已覆盖 capability 与 invalid-response 矩阵；Workbench 不应重复 HTTP 解析细节，只验证 feature 对 adapter 成功/失败结果的用户行为。
- Workbench 多个用例重复 preview → submit → find button 流程，可用窄 helper 降低噪音，并按 restore/submission/clarification/chat 四类 interface 拆分文件。

## Python 侧前端测试

- `tests/unit/test_frontend.py` 142 行、5 个测试。`18-40` 直接读取 nginx/Caddy 配置并断言精确字符串/多行片段，容易因等价配置改写而失败；`80-85` 断言 legacy HTML 包含 literal `Schema Trace`。
- `45-51`、`73-77`、`93-108` 重复创建 `ASGITransport` 与 `AsyncClient`，适合提取窄的 app/client fixture。
- API-only 默认 404、legacy 开关、CORS 与路由仍是必要可观察 contract；部署配置可改为解析/行为校验或最小契约断言，避免把排版当行为。

## 建议 seam

1. `api/client` 和 endpoint adapters：验证 URL、超时、payload validation、稳定错误投影。
2. `jobEvents` adapter：验证 EventSource/reconnect/polling/authoritative read 状态机。
3. Workbench feature orchestration：验证恢复、提交、澄清和 chat 的用户可观察状态；不要断言内部回调装配。
4. FastAPI deployment interface：验证 API-only、legacy opt-in、CORS 与静态路由；部署配置只验证语义。

以上是静态审计候选；删除前必须把每个测试映射到唯一 requirement/interface，确认同一回归保护已由更稳定的 seam 覆盖。
