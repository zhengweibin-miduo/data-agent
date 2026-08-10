# 设计 Query 权威用户规则消歧方案

## Goal

为 PR #85 剩余的唯一 unresolved P1 review thread 提供可实现、可验证的方案：
当用户已经明确确认并持久化“销售额指实付金额”一类业务别名时，后续 Query 可在
普通 Meta 绑定不唯一的情况下复用该规则，而不把普通历史消息、摘要、模型推断或
自由文本记忆提升为 SQL 执行依据。

本任务只完成规划。未经用户再次批准，不运行 `task.py start`，不修改业务实现，
不提交、推送、回复或 resolve GitHub review thread。

## Background

- PR #85 当前 head 为 `5da28eb2047b59e133fca23ccef5aab57151dfdd`；唯一未解决
  thread 为 `PRRT_kwDOTXnY3c6XeklV`，位置为
  `backend/src/query/application/service.py:165`。
- `QueryApplication.stream()` 使用 `include_context=False` 开始轮次，并只从
  `pending_query_chain()` 构造 QueryIntent 证据；因此已完成旧轮次中的 Long-term
  Memory 不进入 Query 绑定。
- 现有 Long-term Memory 已经提供所需 authority：MySQL 权威回查、`user_id` 隔离、
  稳定 UID、`record_version`、`content_hash`、ACTIVE/SUPERSEDED/DELETED 生命周期和
  精确用户原文证据。无需新增第二套规则表、索引或权威状态。
- 当前 `user.business_rule` 使用自由文本 `UserMemoryContent`，没有结构化 alias/target
  语义，不能安全参与自动绑定或相近业务表达的复用。
- 当前 Meta binding 只允许 QueryIntent 原文与候选权威 `name/aliases` 精确匹配；
  候选不唯一时返回一个最高影响澄清。作用域内 Meta 搜索会读取完整候选集合，不用
  展示 Top-K 证明唯一性。
- 原始 Query 设计已经规定：只有唯一权威候选或显式存储的用户规则才能绕过澄清；
  QueryIntent 仍只能包含当前请求和 pending clarification chain 中的逐字用户证据。

## Requirements

### R1. 独立的 Query Binding Rule 类别

- 在现有 Long-term Memory 中新增 `user.query_binding_rule` 类别和
  `QueryBindingRuleContent`；内容固定包含用户确认的 anchor alias、权威 Meta target、
  支持原文和证据消息 UID。
- 规则作用域沿用现有权威槽：`source=data_agent_conversation + user_id + category +
  memory_key`。同一用户、同一别名只能有一个 ACTIVE 版本；新确认值按现有生命周期
  替代旧版本。
- 规则必须携带 `USER_CONFIRMED` trust、支持原文、证据消息 UID、稳定 memory UID、
  record version 和 content hash。普通 `user.business_rule` 自由文本不得参与自动绑定。
- 该类别使用独立 `content_schema=user.query_binding_rule.v1`，生命周期为 PERMANENT；
  不新增表、列、migration、索引服务或配置项。

### R2. 确定性提炼

- 复用现有 `ExtractionCandidate.key/value`：仅当类别为 `QUERY_BINDING_RULE` 时，
  `key` 是用户确认的 anchor alias，`value` 是精确目标名称或别名；代码将其转换为
  结构化 `QueryBindingRuleContent`。
- 代码必须证明 key 和 value 均逐字出现在同一条受支持用户原文中；证据 UID、角色、
  助手结论后的明确用户复述继续遵循现有校验。模型只提议，不能建立 authority。
- `OK`、`是`、`对` 等模糊确认、只出现别名未出现目标、只出现目标未出现别名、
  助手未被用户逐字复述，以及跨租户/跨消息伪造证据均不得生成规则。
- 规则逻辑 key 由代码使用现有 `strip().casefold()` 语义规范化；不得接受模型另外
  发明的 scope、对象 ID 或 schema fingerprint。

### R3. 权威语义召回与 Query-owned 深 interface

- Query application 新增窄 `QueryBindingRulePort`，使用当前 QueryIntent 的完整槽位文本
  对 `user.query_binding_rule` 执行一次有界混合召回；Query 不导入 Memory repository、
  MySQL 表或索引客户端。
- 生产 adapter 复用 Long-term Memory 现有 MySQL exact baseline + ES + Qdrant RRF，且最终
  只返回经过同用户 MySQL authority 回查的 ACTIVE/current-version/content-hash 规则。
- 返回 Query-owned rule candidates，包含 alias、target、memory UID、record version、
  content hash、score、signals 和 degraded targets；不泄漏 ConversationContext、证据
  原文或任意 MemoryDetail。
- exact alias 命中可以仅依赖 MySQL baseline；相近语义自动绑定要求派生检索未降级且
  候选来自 lexical/vector signal。authority 或语义检索不可用时返回稳定 retryable
  错误或 clarification，绝不静默使用不完整候选集。

### R4. 受限规则匹配

- 普通 Meta 绑定已经唯一时保持当前行为，不让历史规则覆盖当前唯一权威绑定。
- 普通绑定不唯一时，先按 normalized exact alias 直接选择规则；无 exact rule 时，
  使用零温度 structured-output Rule Matcher 在权威召回候选中判断当前 slot quote 与
  哪条规则表达同一业务概念。
- Rule Matcher 只能返回当前 QueryIntent 中的原始 slot quote 和候选 allowlist 中的
  memory UID，不能生成 target、对象 ID、新别名、SQL 或置信度。代码必须拒绝未知 quote、
  未召回 UID、同一 slot 多选、规则类别/trust/version/hash 不符和任何额外字段。
- 当模型判定无等价规则、候选冲突无法唯一选择或检索降级时，继续 clarification；
  不要求用户为每个近义表达都先持久化一条 exact alias。

### R5. 只用于消除 Meta 绑定歧义

- 规则 target 必须在当前请求 DDL 的 table/column allowlist 内，通过作用域 Meta
  搜索得到完整权威候选集合，并与候选 `name/aliases` 精确匹配。结合该槽位允许的
  kind 后必须恰好命中一个对象；否则继续返回原有 clarification。
- 规则不得创建 QueryIntent 中不存在的 measure、dimension、filter、sort 或 time
  槽位，不得提供 filter 值、操作符、聚合、Top-N、时间边界、JOIN 或 SQL 文本。
- 自然语言 Metric 仍不能被规则提升为可执行公式；规则只能选择当前 Query 已允许的
  table/column 候选类型。

### R6. 绑定证明与后续门禁

- `QueryContext` 保留原始用户 quote 到 object ID 的 `bindings`，并为规则解析的 quote
  增加最小 rule proof：target、memory UID、record version、content hash。
- SQL 生成和 AST reverse coverage 继续使用原始 QueryIntent quote；规则 target 只用于
  选择对象，不能成为用户原文证据。
- `bindings_are_authoritative()` 对普通绑定继续按原 quote 回查；对规则绑定改用 proof
  target 回查同一当前 DDL Meta 对象，避免最终 revalidation 因 alias 与目标名称不同而
  错误失败。
- 规则 authority 以 Context 构建时 MySQL 返回的不可变版本快照为线性化点。并发更新
  创建新版本不追溯否定已开始 Query；审计和测试可按 memory UID/version 追踪实际输入，
  但日志不得包含规则原文、目标文本、参数值或业务行。

### R7. 范围与兼容约束

- 保持 Query HTTP、NDJSON、SELECT-only 账号、无总结果 LIMIT、全来源结果范围、
  QueryIntent 证据、generation locks、readiness、EXPLAIN 和一次 repair 契约不变。
- 保持 Long-term Memory 的 MySQL authority、历史、软删除、outbox convergence、
  用户数据删除和用户可见 memory API 行为。
- 这是尚未合并的初始 V1，不增加旧数据迁移、双读、双写、compatibility shim 或新依赖。
- 本次不设计 source/schema 专属用户规则。规则保持既有 user-scoped 跨会话语义；
  若目标在当前 DDL 中不能唯一解析，则 fail closed 并澄清。

## Acceptance Criteria

- [x] AC1：用户明确说“销售额指实付金额”后，提炼产生一个 ACTIVE
  `user.query_binding_rule`，其 key/value、用户证据、UID/version/hash 可核验。
- [x] AC2：仅有助手建议、模糊确认、别名或目标缺失、错误角色/消息 UID、跨用户证据
  均不能产生 Query Binding Rule。
- [x] AC3：确认“销售额指实付金额”后，后续新 Conversation 使用“销售金额”“成交额”
  等被 Rule Matcher 判定为同一业务概念的表达时，可复用同一权威规则并唯一绑定当前
  DDL 的“实付金额”，无需逐个保存 exact alias。
- [x] AC4：无规则、模型拒绝等价、候选冲突、多个/零个 Meta target、目标越出当前
  DDL、语义检索降级、authority 失败或非 ACTIVE/错误 trust/version/hash 时不生成 SQL；
  分别保持 clarification 或稳定 retryable error。
- [x] AC5：当前 Meta 原文绑定已经唯一时不被旧规则覆盖；当前 pending clarification
  中的直接用户证据保持现有优先级。
- [x] AC6：Rule Matcher 和规则都不能补出 QueryIntent 未表达的槽位、过滤值、操作符、聚合、Top-N、
  时间范围、JOIN 或 SQL；现有 exact-evidence 和 AST 门禁测试继续通过。
- [x] AC7：规则绑定在 initial/final Meta authority revalidation 中使用 target proof，
  且 SQL validation 仍按原始用户 quote 证明完整槽位覆盖。
- [x] AC8：Memory authority 批量读取严格按 user/category/key/ACTIVE/current content
  version 过滤；同一 alias 的新规则版本替代旧版本，另一用户永远不可见。
- [x] AC9：相关 Query、Conversation extraction、Memory application/MySQL 单元与集成
  测试通过；Ruff、Pyright、非集成 pytest、必要 MySQL integration、配置/Compose、
  `compileall` 和 `git diff --check` 通过。
- [ ] AC10：重新读取 PR #85 thread-aware 状态并形成代码/测试证据；只有获得独立
  GitHub 写授权后才回复或 resolve `PRRT_kwDOTXnY3c6XeklV`。

## Out of Scope

- 把普通 Conversation 消息、summary、assistant 文本或任意 `user.business_rule` 直接
  注入 Query prompt 或当作 QueryIntent 证据。
- 把规则绑定到永久物理 object ID、当前 schema fingerprint 或 Meta index score。
- source/schema/tenant-group 级规则、规则管理新接口、规则优先级 DSL、条件规则、公式
  指标、手工维护同义词图或新的规则数据库。
- 修改 PR #85 其他已解决 review threads、自动提交/推送/回复/resolve。

## Notes

- 方案基于 PR #85 当前 head，而非父 worktree 中的旧 `master`。
- 既有 `user.business_rule` 仍承载一般业务事实；新的 Query Binding Rule 是唯一允许
  自动选择 Meta 对象的用户记忆类别。
