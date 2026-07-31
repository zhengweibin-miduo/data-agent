# Data Agent 前端产品与界面设计

> 状态：Semantic Night Canvas 已实现，并由用户依据真实节点与外键截图确认通过。本文件替代被否决的浅色 Blueprint 宣传页方案。

## 1. 产品命题

- 主题：把 MySQL 物理结构推进为可验证、可复用的语义元数据。
- 用户：负责数据接入和语义口径的数据工程师 / 分析工程师。
- 单一核心任务：在一张持续可见的数据画布上理解当前 schema、补充业务语义，并把 DDL 任务推进到终态。
- AI 边界：AI 只围绕当前 source、DDL、会话和记忆协作；草稿必须由用户确认后才能提交结构化澄清答案。

## 2. 核心用户旅程

1. 在左侧 DDL 面板输入 source 和 MySQL DDL。
2. 点击“预览结构”，后端复用确定性解析器返回真实表、字段和外键关系；画布建立节点，不创建任务。
3. 用户检查画布后点击“生成语义”，收到 202 时只显示已受理。
4. 画布保持可见，底部 Schema Trace 沿公开阶段推进；右侧 AI 可解释当前表列并收集业务背景。
5. 任务等待澄清时，相关节点与问题同时高亮；AI 可起草，用户确认后调用 job answers API。
6. 成功时画布显示真实 API 支持的表/列/指标数量与 DDL 哈希；失败定位到对应业务阶段。
7. 用户可切换到知识记忆，按 source 搜索、查看历史、修正或软删除权威知识。

## 3. 信息架构

- `/workbench`：DDL 输入、schema preview、任务画布、AI 和 Schema Trace 的统一工作区。
- `/workbench/:jobId`：恢复已知任务状态；因 Job API 不返回原始 DDL，恢复态明确禁用 schema preview 和聊天，直到用户重新载入 DDL。
- `/knowledge`：沿用同一应用外壳的知识记忆工作区。

优先级：

- MVP：DDL preview、数据画布、任务状态、澄清确认、AI 协作、知识记忆。
- P1：浏览器本地保存最近 DDL/job 坐标，明确标注为本机记录。
- Future：元数据明细编辑、服务端任务列表、无 DDL 自然语言问数、团队权限。

## 4. 新增后端契约

```text
POST /api/v1/metadata/ddl-preview
Request:  { source, dialect: "mysql", ddl }
Response: { source, tables[], relationships[], table_count, column_count }
```

- `tables[]` 只返回确定性解析结果：稳定 table id/name、列名、类型、nullable、key role。
- `relationships[]` 只返回 DDL 中真实存在的外键 source/target table + column 坐标。
- 沿用现有 DDL 字节、表数、列数和语法校验。
- 不调用 LLM、不写 Redis/MySQL、不创建 job、不生成推测关系。

## 5. 页面布局

### 5.1 桌面工作台

```text
┌ DATA AGENT ─ source: commerce_prod ─ PREVIEW READY ─────────────── [知识记忆] ┐
├───────────────┬──────────────────────────────────────┬───────────────────────┤
│ DDL / SCHEMA  │                                      │ AI · DDL COPILOT      │
│               │   ┌ orders ───────────────┐          │                       │
│ source        │   │ PK id        bigint   │          │ AI: 我识别到订单事实表 │
│ [commerce…]   │   │    customer_id bigint├──────┐   │ 但支付口径仍需确认。   │
│               │   │    total      decimal │      │   │                       │
│ [DDL editor]  │   └───────────────────────┘      │   │ [补充业务背景______] │
│               │                                  │   │             [发送 →] │
│ [预览结构]    │   ┌ customers ────────────┐      │   │                       │
│ [生成语义 →]  │   │ PK id        bigint  │◀─────┘   │                       │
│               │   │    name      varchar  │          │                       │
│ TABLES 2      │   └───────────────────────┘          │                       │
├───────────────┴──────────────────────────────────────┴───────────────────────┤
│ SCHEMA TRACE  ● 解析结构 ── ● 加载知识 ── ◉ 等待澄清 ── ○ 持久化           │
│                “订单金额按支付还是下单口径？” [让 AI 起草] [确认并继续 →]   │
└──────────────────────────────────────────────────────────────────────────────┘
```

布局原则：

- 顶栏 52px，只展示产品、source/job 坐标、状态和工作区切换。
- 左栏 280–320px，可折叠；输入后显示真实 schema outline，不重复做第二套导航。
- 中央画布占剩余主空间，允许横纵滚动；节点自动排布，不做首版自由拖拽/保存坐标。
- 右栏 340–400px，AI 对话始终与当前画布共享上下文。
- 底部 Trace 180–240px，等待澄清时展开问题，不弹全屏模态框。

### 5.2 知识记忆

保留紧凑三栏外壳：左侧 source/query，中间搜索结果，右侧权威详情与版本历史。删除 hero；首屏直接进入搜索操作。

## 6. 标志性视觉元素：Live Lineage Canvas

画布不是装饰性节点图：

- 表节点来自确定性 preview；字段按 DDL 顺序排列。
- 外键连接只画真实关系，使用 SVG 曲线位于节点之下。
- 当前 AI/澄清涉及的表和字段使用 Semantic Violet 轮廓高亮。
- 任务阶段推进时，一条 Data Cyan “信号线”从左侧 DDL 入口穿过画布，汇入底部 Schema Trace；reduced motion 下只切换颜色，不移动。
- 没有关系的表仍是独立节点，不制造推测连线。

这张活的 lineage 画布同时承担结构理解、问题定位和产品识别，不使用机器人头像、渐变球、营销插画或 KPI 卡片。

## 7. 设计语言：Semantic Night Canvas

### 颜色（6 个核心 token）

- **Canvas Ink — `#08111F`**：主画布和应用背景。
- **Node Slate — `#13233A`**：表节点、侧栏和浮层表面。
- **Data Cyan — `#38BDF8`**：真实关系、当前数据流、焦点。
- **Semantic Violet — `#8B7CFF`**：AI 语义、澄清关联和选中节点。
- **Metric Amber — `#F3B64C`**：等待人工确认、指标与 pending。
- **Ice Text — `#E8F0F7`**：主要文本；次级文本通过透明度形成层级。

错误使用独立系统状态红，不作为品牌 token。所有正文达到 4.5:1，焦点和非文本状态达到 3:1；状态同时使用文字、图标和形状。

### 字体

- UI/标题：`Segoe UI Variable`, `Microsoft YaHei UI`, system-ui；紧凑、清晰，不使用展示型巨字。
- DDL/坐标/字段：`Cascadia Code`, `IBM Plex Mono`, ui-monospace。
- 页面标题不超过 24px；节点标题 13–14px；正文 13–15px；坐标 11–12px。

### 形态

- 半径 6–10px，只用于节点和可操作浮层；画布、轨迹和导航不做卡片堆叠。
- 1px 结构线 + 局部发光承担层级；不使用大面积渐变。
- 信息密度接近数据库 IDE，而不是营销官网或运营 Dashboard。

## 8. 关键状态

- DDL：空白、预览中、语法错误、超限、preview ready、preview stale（DDL 修改后）。
- 画布：空态、单表、多表无关系、多表有关系、超宽/超高、字段长名称。
- 任务：pending、running、waiting_input、succeeded、rejected、failed、404/过期。
- 连接：SSE 在线、重连、轮询回退。
- 聊天：创建会话、生成中、readiness 固定回复、模型失败、同 turn 重试、恢复任务无 DDL。
- 记忆：未搜索、空结果、加载、版本冲突、修正、软删除。

## 9. 响应式与可访问性

- `>= 1200px`：左栏 + 画布 + AI 三栏，Trace 固定底部。
- `768–1199px`：AI 变为右侧可开关面板；DDL 左栏保持。
- `<768px`：source/DDL、画布、Trace、AI 顺序堆叠；画布最小高度 440px，可独立二维滚动。
- 所有节点可通过键盘顺序聚焦；聚焦节点时关联线加粗，屏幕阅读器读出表名、字段数量和关系数量。
- SVG 关系线 `aria-hidden=true`，同一关系以节点内文本提供等价信息。
- 状态更新使用 `aria-live=polite`；错误摘要可聚焦；弹窗使用原生 dialog。
- 支持 200% 文本缩放、44px 触控目标、visible focus 和 reduced motion。

## 10. 自审

- 已彻底删除被否决的巨型 hero、宣传页留白、细弱浅色层级和说明性装饰图。
- 数据画布所需结构由真实后端 parser 提供，不复制 SQL parser，也不伪造关系。
- 视觉风险只花在 Live Lineage Canvas；导航、表单、聊天和记忆保持安静、紧凑。
- 首版不做自由拖拽、缩放工具条、小地图、自动保存布局或复杂图算法；节点超过可视区时使用原生滚动。只有真实使用证明需要后再添加。
