# Verification

## Result

- 独立 `trellis-check` 发现并修复 1 处 Readiness 语义漂移：无需完整 DW 数据的问题会直接放行且不访问 `data_sync`；只有依赖 DW 的问题才要求 `streaming` 且心跳新鲜。
- 同步修正 `docs/agent-knowledge.html`、`chat-turn-sequence.svg` 与同名 `@2x.png`，避免正文和图示暗示所有 Chat 都必须等待 Data Sync。
- 页面覆盖 PRD 要求的七类知识，并明确 Chat HTTP / DDL Job SSE、Readiness 非 DW 查询工具、10 节点工作流、六态 JobStatus、Accepted Snapshot 事务、Data Sync 与两类 Projection。
- 未修改生产代码、API、数据库或运行配置。

## Fresh checks

- Python `HTMLParser`：HTML 可解析，目录锚点唯一且目标存在，SVG 相对路径存在。
- Python `ElementTree`：SVG XML 可解析。
- 内容断言：必需边界、状态、进程、路径存在；旧的 9 节点、三态 JobStatus 和历史路径描述不存在。
- 图示断言：生成、分类、抽取三条共享模型调用各一条；PNG 大于 100 KB。
- 离线断言：HTML/SVG 不引用 `https://` 外部资源。
- Playwright：1440px 与 390px 下 `body.scrollWidth == documentElement.scrollWidth == innerWidth`，无页面级横向溢出；图示只在自身 `overflow:auto` 容器中滚动。
- Playwright 放大交互：5 个按钮逐一验证正确标题和图片；关闭按钮、Esc、点击遮罩、焦点返回全部通过。
- Web Interface Guidelines：2026-08-03 重新获取上游 `command.md`；原生语义按钮/对话框、可见焦点、reduced motion、图片显式尺寸与懒加载、skip link、modal overscroll 与触摸反馈均通过复查。
- 最终图片查看器：入口为图片右上角四角展开 icon-only 按钮；Playwright 验证滚轮 `100% → 110% → 100%`、`− / +`、50%–300% clamp、焦点返回与移动端平移均通过。
- 中心缩放：Playwright 在鼠标位于左上角时触发滚轮，验证滚动偏移仍按当前可视区域中心的 1.1 倍比例更新；`− / +` 使用同一中心锚点。
- 抓取拖拽：Playwright 在 150% 缩放下验证 `pointerdown → pointermove` 同时改变 `scrollLeft/scrollTop`，拖动 class 在按下/松开时正确添加/清除；查看区域聚焦后方向键仍可平移。
- Ruff：`uv run ruff check src tests` 退出码 0。
- Pyright：`uv run pyright src tests` 退出码 0。
- Frontend lint：`npm run lint` 退出码 0。
- Frontend typecheck：`npm run typecheck` 退出码 0。
- `git diff --check`：退出码 0；仅有 Git 的 LF→CRLF 工作副本提示。

## Visual artifacts

- `docs/diagram/agent-knowledge/agent-system@2x.png`：3280×1800。
- `chat-turn-sequence@2x.png`、`ddl-job-lifecycle@2x.png`、`snapshot-convergence@2x.png`、`conversation-memory@2x.png`：均为 2320×1520。
- 桌面/窄屏验证截图位于 Codex 临时可视化目录，不进入仓库。

## Spec judgment

本任务没有新增或修改 API、数据、基础设施或跨层运行契约，只把已有代码、`CONTEXT.md`、`CONTEXT-MAP.md` 与现有 `.trellis/spec/` 同步到知识页；没有新的可执行规范需要写回 `.trellis/spec/`。

## Not run

未运行后端 pytest、前端 unit test/build 或 live integration：本任务仅修改独立文档 HTML/SVG/PNG，不修改生产代码或构建输入；HTML/SVG/PNG 静态断言与真实浏览器交互回归直接覆盖本次风险。
