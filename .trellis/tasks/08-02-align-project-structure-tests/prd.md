# 按架构规则重构项目结构与测试

## Goal

使仓库的实际项目结构、前后端分离边界和测试组织符合项目已规定的架构规则；在保持可观察行为与跨端契约正确的前提下，降低测试重复、内部实现耦合和维护成本。

## Background

- 用户要求纠正不符合项目规则的结构、DDD/Ports-and-Adapters 依赖和臃肿测试。组合 Skill 复审的完整证据位于 `research/architecture-combination-review.md`。
- 独立 `frontend/`、默认 API-only FastAPI、显式 legacy 开关、集中 API/SSE adapter 和前端类型投影已经符合前后端分离规则；本任务不得重复拆分已分离的工程（`research/structure-boundary.md`、`research/onboarding-frontend-flow.md`）。
- 当前后端只能判定为“局部、渐进式落实 DDD”：`memory.application`、`conversation`、`data_sync`、`answer_readiness` 等内层仍直接依赖 infrastructure、具体 repositories 或全局配置；部分 bounded context 共享具体持久化实现（`research/import-graph-audit.md`）。
- Python 导入图没有解释器级环，但存在 `data_sync.backfill → metadata_indexing.value_refresh → data_sync.models/tables` 结构性依赖环。已决定把 `metadata_indexing` 定义为 DDL Metadata 内的 **Meta Projection**；Meta Snapshot 保持权威，Data Sync 只通过稳定 read port 或 projection event 提供值数据。统一语言和关系记录在 `CONTEXT.md`、`CONTEXT-MAP.md`。
- 前端主要问题是 `WorkbenchPage.tsx` 集中 restore、submission、SSE、chat、clarification 等状态机，URL 权威状态还在 shell 与 feature 间形成维护 seam；不是源码根目录混放（`research/frontend-architecture.md`）。
- 后端测试审计记录约 19,758 行/373 tests，主要臃肿点是 Meta Projection 三个 800+ 行文件和 `data_sync/test_service.py` 对私有协作者的 mock/call-order 断言；前端约 1,721 行/73 tests，`WorkbenchPage.test.tsx` 为 706 行/34 tests，并与 API/SSE adapter 重复覆盖部分机制（`research/backend-test-audit.md`、`research/frontend-test-audit.md`）。
- 已确认六类公共测试 seam：DDL Job lifecycle、Accepted Meta Snapshot 发布、Conversation/Long-term Memory、Data Sync task lifecycle、Meta Projection lifecycle/search、前端 transport/feature orchestration。交付采用一个父任务和四个纵向子任务，测试随各架构改动执行，不建立横向测试任务。

## Requirements

- 核验根目录、`frontend/`、`src/data_agent/`、`contracts/`（如存在）及测试目录的真实所有权和依赖方向。
- 识别并纠正前端业务源码、后端业务源码、兼容资源和跨端契约放置不当的问题。
- 纠正已确认的后端内层到 infrastructure 的反向依赖，并为跨 bounded context 的 metadata indexing 建立明确 interface/seam，而不是直接共享持久化表实现。
- 明确区分“DDD 目标规则”和“当前符合程度”；设计与验收必须逐项说明受影响 bounded context、domain/application/adapters/infrastructure 职责、port/adapter seam 和依赖方向，不得用目录重命名代替 DDD 落地。
- 为 DDL lifecycle、accepted snapshot、Conversation/Long-term Memory、Data Sync lifecycle、Metadata Projection、frontend transport/feature 明确公共测试 seam；新 interface 覆盖后必须删除被替代的私有 helper/collaborator 测试，禁止叠加两套重复测试。
- 将 Workbench feature 的生命周期编排从单一页面模块中提炼为更深的 feature 内部 module，同时保持页面公共行为和 URL/session 状态所有权不变。
- 保持前后端仅通过显式 HTTP、SSE 或契约交互，禁止跨端直接依赖对方内部实现。
- 以公共 seam 和可观察行为为测试边界，识别重复覆盖、内部实现 mock、过宽 fixture、超大测试文件和无价值参数化。
- 在不降低关键行为覆盖与回归保护的前提下，合并、拆分或删除冗余测试，并明确每类测试的职责。
- 避免为尚不存在的历史数据增加迁移、回滚或兼容清理路径；如仓库证据显示存在真实兼容要求，则在设计阶段单独说明。

## Acceptance Criteria

- [ ] 每个结构或依赖方向问题都有可核验的文件、符号或配置证据，并映射到项目架构规则。
- [ ] 对后端 DDD 符合程度给出明确结论，并证明受影响模块的内层不再依赖具体 infrastructure、不同 bounded context 不再直接共享对方持久化实现。
- [ ] `metadata_indexing` 的领域归属、权威状态、输入/输出关系和依赖矩阵被明确记录，Data Sync 与 Metadata Projection 之间不再存在双向 implementation 依赖。
- [ ] 前端、后端、兼容资源和跨端契约分别具有单一、明确的源码所有者。
- [ ] 受影响的运行入口、构建/打包、静态资源挂载及测试发现路径在重构后仍可工作。
- [ ] 测试重构后关键公共行为仍有覆盖，重复覆盖和对内部实现的脆弱耦合得到实质减少。
- [ ] 相关 Ruff、Pyright、pytest 与前端检查命令通过；无法运行的检查必须记录原因。
- [ ] `design.md` 明确目标边界、依赖规则、契约与兼容策略，`implement.md` 给出分阶段清单、验证门禁和回滚点。

## Task Map

- `refactor-memory-conversation-boundary`：建立 Memory/Conversation application ports，消除内层和跨 context 对具体 persistence/infrastructure 的依赖，并替换对应测试。
- `refactor-meta-projection-boundary`：落实 DDL Metadata 对 Meta Projection 的所有权，重塑 accepted snapshot publication seam，并替换对应测试。
- `refactor-data-sync-ports`：为 Data Sync 建立 application ports，消除 Data Sync 与 Meta Projection 的双向 implementation 依赖，并替换对应测试；实现依赖前一子任务确定的 projection interface。
- `refactor-workbench-modules`：提炼 Workbench feature 内部 module/seam，保持跨端契约与用户行为，并替换重复或实现耦合的前端测试。

父任务只负责需求、领域关系、依赖顺序和最终跨子任务验收，不直接修改业务源码。
