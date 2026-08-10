# 修复 PR #85 未解决的查询正确性与执行所有权问题

## Goal

消除 PR #85 当前 5 个 unresolved P1 review threads 所揭示的查询正确性和并发
所有权缺陷，使自然/相对时间问题能够安全形成 SQL，长澄清链不丢失原始证据，过期
执行者不能影响新领取者，并且所有会决定修复或终态的 `EXPLAIN` 都受同一 generation
读协调保护。

目标 PR 为 `zhengweibin-miduo/data-agent#85`，base 为 `master`；本任务从已核验的
PR head `ccb9c02d1b8ce2340f9c9270f8b23e29941607ed` 创建。

## Background

- GitHub 当前共有 227 个 review threads，其中 5 个 unresolved；4 个仍在当前 diff，
  1 个已经 outdated 但因明确标记“无法安全完成”而继续保持 unresolved。
- 时间相关的两个 thread 指向同一根因：`QueryIntent.time_filter` 仍是单个
  `FilterIntent`，只能承载一个操作符和值，而自然年份、月份和相对范围需要由可信
  代码派生两个边界；模型既不能伪造原文中不存在的操作符，也不能伪造具体日期。
- `QueryApplication.stream()` 从普通 bounded Conversation context 重建 pending
  clarification chain；生产窗口只有 20 条消息和 32,768 字符，长链会丢失原始问题。
- Conversation 轮次租约只以 `turn_uid` 区分 owner。过期重领后，旧执行者仍可按同一
  UID 续租、完成或 abandon 新执行者的轮次。
- Query 已在最终 readiness、关系复核、`EXPLAIN` 和流式读取期间使用 Locking
  Service READ lock，但 `_plan()` 中影响 repair/终态的首次 readiness 与
  `EXPLAIN` 仍可发生在协调区外。
- 既有任务 `08-07-query-generation-read-coordination` 已实现/规划 shared READ、
  exclusive WRITE、原子多目标获取和启动 capability probe。本任务复用该协议，
  不重新设计另一套 generation 锁。
- 项目仍按初始 V1 处理；`docs/docker/mysql/` 是全新环境 bootstrap，当前没有升级
  migration framework。本任务不为不存在的旧数据增加迁移、兼容、回滚或清理路径。

## Requirements

### R1. 可信自然时间范围

- 将自然时间范围建模为可表达起止边界的 Query-owned 可信契约；保留用户原文证据，
  具体边界只能由确定性代码和注入时钟派生，不能由 LLM 自报。
- 用户时区由 Query 请求新增的 Query-owned `supplemental_context.user_timezone`
  显式携带，使用 IANA 时区标识；不得把用户时区塞入共享的 `DDLJobRequest`，也不得
  从服务端全局默认值、用户画像、服务器本地时区或 MySQL session 时区推断。
- 时区必须进入 Query 的语义幂等 fingerprint；相同 `turn_uid` 使用不同用户时区时
  必须判定为 idempotency conflict，不能回放另一时区下的结果。
- 注入时钟提供权威 UTC instant；确定性归一化先转换到补充上下文携带的用户时区，
  计算自然边界，再转换为与固定 UTC Query/MySQL session 一致的绑定参数。
- 至少支持 review 明确覆盖的绝对年份、自然月份、`今年`、`去年`、`本月`、`上月`
  和 `最近 N 天`，并使用半开区间避免年度、月份和 DATETIME/TIMESTAMP 日边界错误。
- `最近 N 天` 固定解释为用户时区下包含今天的 N 个自然日：起点为用户当地
  `today - (N - 1)` 的 00:00，终点为当地明天 00:00，形成左闭右开区间；不得解释
  为从当前 instant 向前滚动 N×24 小时。
- 时间字段不明确时仍只澄清该字段；用户回答后，原始时间范围与新时间字段证据必须
  合并成同一 pending chain，并能进入验证及执行路径。
- SQL AST、参数和值类型门禁必须精确验证两个边界及其绑定字段；不降低逐字证据、
  参数化 SQL、权威物理字段类型或目标表 allowlist。
- 不把普通字符串过滤扩展为隐式日期推断，也不支持本任务未列出的模糊自然时间表达。

关联 threads：

- `PRRT_kwDOTXnY3c6XW7Rp`：澄清时间字段后自然范围仍无法形成谓词。
- `PRRT_kwDOTXnY3c6XctZA`：相对时间范围无法形成可执行边界。

### R2. 独立且持久的 pending clarification chain

- Query 必须通过 Conversation 的显式 port 加载当前 pending clarification chain，
  不得依赖普通摘要后的 20 条/32,768 字符上下文窗口偶然保留原始问题。
- chain 只包含当前未结束 Query 的原始用户问题、Query clarification 和后续用户回答；
  不得混入任意已完成轮次，也不得把 assistant 文本当成逐字用户证据。
- 该读取必须有独立、可配置或明确常量化的消息数和字符安全预算；超预算、断链或读取
  失败时 fail closed，不能按缩水后的意图继续执行 SQL。
- 非 clarification 终态关闭 chain；幂等回放继续保留原终态类型。

关联 thread：`PRRT_kwDOTXnY3c6XXNaf`。

### R3. 带 fencing 的轮次领取所有权

- 每次首次 claim 或租约到期后的 reclaim 都生成新的不可猜测 claim token 或单调
  ownership version，并把它作为执行代次坐标返回给调用方。
- `renew_turn`、首次 `complete_turn` 和 `abandon_turn` 必须携带该坐标并通过 CAS；
  旧代次在新代次领取后恢复时，这三类操作都不得改变新 owner 的状态。
- Conversation application/store ports、Query heartbeat/finally、Chat 以及其他活动轮次
  调用方必须端到端传递同一坐标；不得只修 Query 局部调用而保留可绕过入口。
- 已完成轮次的幂等回放保持不重复执行；不同内容复用 `turn_uid` 仍返回现有冲突。
- 若需要新增数据库列，只更新初始 V1 schema/bootstrap 与对应模型测试，不新增升级
  migration、旧行 backfill 或双版本兼容分支。

关联 thread：`PRRT_kwDOTXnY3c6Xcmf9`。

### R4. generation 协调覆盖所有决定性 EXPLAIN

- 所有会决定 model repair、readiness 终态或请求错误分类的 readiness、权威关系复核和
  `EXPLAIN` 必须在对应目标表的同一组 Locking Service READ locks 内完成。
- 不允许保留任何锁外数据库 `EXPLAIN` 预检；如果 repair 改变目标表，必须对修复稿
  重新获取正确的目标集合并重新完成协调检查。
- 长流使用的 generation owner 连接与普通业务/查询执行连接必须有明确资源所有权、
  容量门禁和应用生命周期，避免池耗尽或 owner 连接被业务路径复用。
- accepted snapshot、schema sync 与 generation reset 的 WRITE 语义、锁名、原子多目标
  获取和稳定错误映射保持不变。

关联 thread：`PRRT_kwDOTXnY3c6XUMt9`（outdated 但 unresolved）。

### R5. 范围和交付约束

- 采用公共 seam 上的测试优先纵向切片：先证明失败，再实现最小契约，再补跨层回归。
- 更新受影响的 Query/Conversation 规范，使新契约成为后续实现和 review 的唯一来源。
- 当前阶段只创建和规划任务；未获得后续实现审批前不得运行 `task.py start` 或修改业务
  实现。
- 本轮授权不包含 commit、push、回复 GitHub 评论或 resolve review threads。

## Acceptance Criteria

- [ ] AC1：固定时钟测试证明绝对年份、自然月份、今年/去年、本月/上月和最近 N 天
  产生精确半开区间；无原文时间证据、无时间字段或不支持表达均 fail closed。
- [ ] AC1a：HTTP/Application 契约验证 IANA 用户时区并端到端传递；缺失或非法时区
  不得回退到服务端/数据库默认值，相同 `turn_uid` 改变时区产生幂等冲突。
- [ ] AC2：从“含自然时间范围的原问题 → 系统澄清时间字段 → 用户短回答”完整两轮流程
  能生成与执行通过确定性门禁的参数化 SQL。
- [ ] AC3：超过普通 20 条消息窗口和 32,768 字符预算后，独立 pending chain 仍保留
  原始指标、过滤、结果形态和时间范围；超过 chain 自身安全预算时返回稳定安全错误，
  不执行 LLM repair、`EXPLAIN` 或 SELECT。
- [ ] AC4：并发测试证明旧 owner 在租约过期、新 owner reclaim 后，续租、完成和
  abandon 均 CAS 失败且不影响新 owner；新 owner 可正常完成。
- [ ] AC5：Chat 和 Query 的正常完成、失败清理、取消、心跳及幂等回放测试均使用并
  验证 claim coordinate，不能存在只按 `turn_uid` 的活动 owner mutation。
- [ ] AC6：MySQL 并发集成测试证明首次 readiness 通过后 WRITE owner 开始 schema
  同步时，Query 的决定性 `EXPLAIN` 不与 DDL 竞态；结果只能是稳定读取、准备中或
  generation lock unavailable，不能误报 query timeout/database failure 或消耗错误
  repair 预算。
- [ ] AC7：repair 改变目标表时重新获取正确 READ lock 集合并重新验证；取消、异常和
  release 失败不泄漏 owner 连接，也不覆盖原始业务异常。
- [ ] AC8：协调连接容量测试证明并发长流不会耗尽普通 MySQL/Query 执行连接池，启动与
  关闭生命周期完整释放专用资源。
- [ ] AC9：受影响的 Query、Conversation、infrastructure 单元/集成测试，以及 Ruff、
  Pyright、`compileall`、配置加载、构建、Compose/CI 静态检查和 `git diff --check`
  通过；外部服务不可用时如实报告未执行项。
- [ ] AC10：实现完成后重新读取 PR #85 thread-aware 状态，逐条给出提交与验证证据；
  只有获得明确 GitHub 写授权后才回复或 resolve。

## Out of Scope

- 新增数据库 migration framework、旧数据 backfill、双版本 schema 兼容或历史清理。
- 改变 SELECT-only 账号、无总结果 LIMIT、NDJSON 批次或全来源结果范围。
- 支持任意自然语言日期、节假日、财务季度、locale 猜测或用户画像时区推断。
- 重写已经完成的 Locking Service shared/exclusive 协议或降级回 `GET_LOCK()`。
- 自动提交、推送、创建新 PR、回复或 resolve GitHub review threads。

## Notes

- 这是复杂跨层任务；必须补齐 `design.md`、`implement.md`、真实
  `implement.jsonl` 和 `check.jsonl`，完成 PRD convergence 并经用户审阅后，才能运行
  `task.py start`。
