# 更新 Agent Knowledge 页面

## Goal

完整刷新 `docs/agent-knowledge.html`，使它成为与当前 `master` 代码一致、可直接打开阅读的 Agent 系统知识页。

## Background

- 现有页面是 2026-07-27 的快照，后端现已迁移到 `backend/src/`，前端由 `frontend/src/` 独立拥有。
- 当前页面遗漏 `chat`、`answer_readiness`、`data_sync`、`ddl_metadata/meta_projection` 与独立前端工作台，并保留多处已经失真的路径、状态和运行语义。
- 当前统一语言包含 DDL Metadata、Conversation、Long-term Memory、Meta Snapshot、Memory Projection、Meta Projection；上下文地图还确认 Data Sync 是独立 bounded context。
- 用户已确认按当前 `master` 做完整内容刷新，而不是只修路径。
- 用户复查后认为当前版本仍偏术语堆叠：需要保留技术准确性，但把机制、因果和边界解释得更详细易懂，并用具体例子说明每项能力解决的问题。

## Requirements

### R1：当前 Agent 主流程

- 页面必须把 Workbench、Conversation/Chat、回答就绪门禁和 DDL Copilot 串成真实请求流。
- 必须明确 Chat 使用普通 HTTP 长请求；SSE 仅用于 DDL Job 事件，不得暗示 token streaming。
- 必须明确 readiness 只决定是否允许继续生成回复；当前没有 DW 业务行查询或 SQL 执行工具。

### R2：DDL Metadata 与异步任务协议

- 页面必须覆盖 DDL 预览、Redis 原子受理、立即入队与 dispatch outbox 兜底、arq worker、10 节点 LangGraph、sync checkpoint 恢复、人工澄清和完整六态 JobStatus。
- `persist_snapshot` 仅作为 DDL Metadata 工作流的唯一成功出口描述，不得扩大成全项目唯一写入或唯一控制流。

### R3：权威状态、同步与投影

- 页面必须区分 Meta Snapshot、Long-term Memory、Data Sync desired state、Meta Projection 和 Memory Projection 的所有权。
- accepted snapshot 必须描述为 generation locks 下的单一 MySQL 事务，包含 Meta、Memory、Data Sync desired state 以及 Meta/Memory projection outbox。
- Data Sync 必须覆盖独立 CDC 进程及 `PENDING_SCHEMA → BUFFERING → BACKFILLING → REPLAYING → STREAMING` 主阶段，并说明异常终态。
- MySQL 是权威源；ES/Qdrant 是可重建投影。

### R4：当前模块边界

- 使用当前 `backend/src/`、`frontend/src/` 路径。
- 按 module、interface、seam、adapter 描述 application 与 adapters 的职责，不把模块内部契约误写成全项目共享契约。
- 修正 Memory 检索、修正/删除、投影调度、Conversation turn lease 与证据抽取等已经变化的语义。

### R5：交付形式

- 维持独立静态 HTML，不引入站点框架、构建链或新依赖。
- 保持中文、响应式目录、浅色/深色兼容和浏览器直接打开能力。
- 总架构与四个专题流程分别使用职责单一的自包含 SVG，避免在 HTML 中维护难以复用的内嵌图；生成的 PNG 仅作为视觉校验产物。

### R6：架构图放大查看

- 架构图片右上角必须叠放通用的四角展开（Maximize）图标按钮，不显示“放大查看”文字。
- 放大视图使用浏览器原生对话框，支持关闭按钮、Esc 和点击遮罩关闭，并在窄屏上允许平移查看完整 SVG。
- 弹窗内鼠标滚轮必须围绕当前可视区域中心放大/缩小；同时提供 `− / +` 键盘按钮和百分比反馈。
- 缩放必须有上下限，控件可键盘操作并有可见焦点，不引入图片查看库。
- 放大后支持按住鼠标或触摸拖拽平移；查看区域可聚焦，并保留方向键平移作为键盘替代方式。

### R7：详细解释、问题示例与多图导读

- 每个核心章节按“要解决的问题 → 具体例子 → 处理机制 → 状态/数据落点 → 关键边界 → 代码证据”组织，不能只列术语、路径和状态。
- 保留准确的技术术语；DDD、outbox、projection、lease、generation 等首次出现时必须解释其在本项目中的具体职责和因果关系，而不是替换成模糊口语。
- 示例必须包含明确输入、系统所处状态、关键处理步骤和可观察结果，并说明为什么简单做法会产生错误或一致性风险。
- 在总架构图之外增加按读者问题组织的流程图，分别解释一次聊天请求、一次 DDL 任务、Snapshot 后的异步同步/投影，以及 Conversation 与长期记忆如何协作。
- 图与正文必须共享同一套颜色、角色名称和箭头语义；每张图只讲一个主题，并提供简短图注和文字版结论。

## Acceptance Criteria

- [ ] 页面覆盖当前 Agent 主流程、DDL Job、Readiness、Data Sync、Meta Projection、Long-term Memory/Conversation、运行边界七类核心知识。
- [ ] 页面中的关键路径、符号、步骤数、状态名和进程名均能在当前仓库中找到证据。
- [ ] 页面明确写出 Chat HTTP / DDL SSE 分流，以及 readiness 不等于 DW 查询能力。
- [ ] 页面不再出现 `9 个节点`、三态 JobStatus、`memory/indexing/dispatcher.py`、`ddl_metadata/persistence/snapshots.py`、`MemoryService.add` 等旧描述。
- [ ] HTML 与 SVG 可在无后端服务、无网络资源时直接渲染；目录导航正常。
- [ ] SVG/PNG 通过视觉检查，无重叠、裁切或不可读文本。
- [ ] 右上角四角展开图标可打开架构图对话框，关闭按钮、Esc 与遮罩关闭均正常，关闭后焦点返回触发按钮。
- [ ] 弹窗内滚轮与 `− / +` 均可在 50%–300% 范围缩放，百分比同步更新，缩放后仍可平移查看。
- [ ] 放大图支持鼠标/触摸抓取拖拽，拖动时有明确光标反馈，键盘方向键仍可平移。
- [ ] 不熟悉本项目的研发读者通过章节说明、例子、图和图注，能说明 Chat、DDL Job、Data Sync、Projection、Conversation/Memory 各自解决什么问题、如何处理以及彼此如何连接。
- [ ] Chat/Readiness、DDL 人工澄清、Accepted Snapshot 后异步收敛、Conversation 证据抽取与长期记忆至少各有 1 个具体示例，且示例不夸大当前系统能力。
- [ ] 页面除总架构图外至少包含 4 张专题图；每张图聚焦单一流程，在桌面和 390px 窄屏下均可读、可横向滚动且不撑宽页面。
- [ ] 改动只包含任务元数据、知识页和其图示资产，不改生产代码、API、数据库或运行配置。

## Out of Scope

- 实现 DW 查询、SQL 工具、token streaming 或新的 Agent 能力。
- 修改现有前后端行为、接口契约或基础设施。
- 建设完整文档站点或新增文档生成器。
