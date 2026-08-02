# Conversation 与 Long-term Memory 数据流（Phase 2）

## 事实与调用链

- HTTP 入口由 `src/data_agent/conversation/api.py` 暴露会话/消息/用户记忆路由（模块导入 `ConversationService` 与 `MemoryService`，见 api.py:17-19）。聊天入口 `src/data_agent/chat/api.py:17-26` 调用 `ChatService.run_turn`；`chat/service.py:84-94` 先调用 `ConversationService.start_turn`，完成时调用 `complete_turn`（service.py:133-145）。
- Conversation bounded context 的应用编排在 `conversation/service.py`。所有读写通过 `MySQLDatabase.session()` 短事务托管：创建/列表/历史/删除分别在 service.py:32-103；`start_turn` 在 service.py:105-131 先提交用户消息和活动轮次，再事务外构建上下文；`complete_turn` 在 service.py:133-150 提交助手消息及提炼任务后才返回。
- 会话聚合/权威状态位于 MySQL `agent_conversation`、`agent_message`。`ConversationRepository.start_turn` 先 `get(... for_update=True)` 并校验 `active_turn_uid` 门禁（repository.py:272-308），随后在同一事务插入用户消息并写入门禁（repository.py:344-364）。`complete_turn` 锁会话、校验用户消息与当前门禁，再同事务插入助手消息、写 `conversation_memory_outbox`、清除 `active_turn_uid`（repository.py:366-474）。因此消息、轮次门禁和助手完成是 Conversation 的权威状态。
- 上下文组装在 `ConversationService._context`（conversation/service.py:179-238）：MySQL 读取摘要游标后的消息（service.py:194-207），随后调用 `MemorySearchService.search`，携带 `CONVERSATION_MEMORY_SOURCE`、用户 ID、用户画像/偏好/约束/业务规则类别（service.py:210-226）。会话删除只删除会话；注释明确长期记忆跨会话共享（service.py:82-103）。
- `delete_user_data` 是跨上下文删除用例：同一 MySQL session 内先 `ConversationRepository.delete_user_conversations`，再直接 `MemoryRepository.tombstone_user`（conversation/service.py:166-177）。Memory tombstone 将用户记忆置 `DELETED`、写删除事件并为全部 UID 写双目标 DELETE outbox（memory/mysql/repository.py:1264-1325）；物理 purge 仅在索引删除确认后执行（repository.py:1327-1378）。

## Long-term Memory 权威链路

- Memory 写模型 `MemoryRepository.upsert_candidates` 以 MySQL `agent_memory` 为权威，事务内校验类别策略、生命周期/活动槽和 tombstone，并同时写历史/关联及双目标 outbox（memory/mysql/repository.py:204-260 及其后续更新逻辑）。更新/软删同样先改 MySQL 权威行，再写 `agent_memory_event` 和 outbox；软删证据见 repository.py:1225-1262。
- 对话提炼由 `ConversationMemoryExtractor`（conversation/extraction.py:170-203）领取 `conversation_memory_outbox`。领取在短 MySQL 事务后，模型调用在事务外（extraction.py:187-203、215-233）；模型结果经 `_validated_candidates` 按消息 UID/角色/逐字 quote/时序校验（extraction.py:58-167），新事务中调用 `MemoryRepository.upsert_candidates`，再 `ConversationRepository.finish_extraction` 原子完成摘要与 outbox（extraction.py:235-247）。失败释放租约并退避重试（extraction.py:248-257）。
- `MemorySearchService.search`（memory/application/search.py:45-）将 MySQL 精确候选与 Elasticsearch BM25、Qdrant 向量并发检索；ES/Qdrant 仅返回候选 UID，随后 `MemoryRepository.get_many_active(... user_id=...)` 回查 MySQL 权威行，并读取 `MemoryIndexOutboxRepository.pending_targets`（search.py:114-145）。待处理目标会剔除对应排名信号（search.py:146-166），最终再次校验 source/user/status/content_version/hash/expiry/object 白名单（search.py:168-217），所以派生索引不是内容权威。
- `MemoryIndexDispatcher`（memory/indexing/dispatcher.py:72-96）短事务领取 outbox，事务外写 ES/Qdrant/TEI，独立短事务确认或退避（dispatcher.py:113-155）。删除或非 ACTIVE 权威行统一收敛为删除（dispatcher.py:52-69）；外部写入期间权威变更则不确认并补排 convergence outbox（dispatcher.py:138-153）。

## bounded context 关系、seam 与适配器

- Conversation 的 driving adapter 是 HTTP routers；application seam 是 `ConversationService`，driven adapter/repository 是 `ConversationRepository` + MySQL session。Memory 的 application seam 是 `MemoryService`/`MemorySearchService`，driven adapter 是 `MemoryRepository`、`MemoryIndexOutboxRepository`，外部索引适配器为 `MemoryElasticsearchIndex`、`MemoryQdrantIndex`，基础设施为 MySQL/ES/Qdrant/TEI clients。
- 合法协作方式：Conversation 通过 `MemorySearchService` 查询同用户长期记忆；提炼通过 `MemoryCandidate` 写入 Memory；两者以用户 ID、source、UID 和 outbox 事件协作，而非共享 ORM 实体。

## 具体依赖与 DDD 风险（事实）

- `ConversationService` 直接导入并实例化 Memory bounded context 的具体类 `MemorySearchService`、`MemoryRepository`（conversation/service.py:15-20），并在 `delete_user_data` 中直接调用 `MemoryRepository.tombstone_user`（service.py:166-177）。这是跨上下文对 concrete repository 的依赖，绕过 application port/防腐层；若按项目渐进 DDD 规则，属于边界泄漏。
- `ConversationMemoryExtractor` 同时直接依赖 `MemoryRepository` 及 Memory domain payload/policies/models（conversation/extraction.py:23-37、238-243），在 Conversation worker 内直接执行 Memory 写模型；这是跨上下文具体实现依赖，且提炼事务同时更新 Conversation 摘要和 Memory 权威行，形成跨聚合事务耦合。当前代码通过同一 MySQL session 保证原子性，但未见显式跨上下文 port/interface。
- `MemorySearchService` 直接构造基础设施客户端及具体索引适配器（memory/application/search.py:9-17、74-113），应用层因此依赖 adapters/infrastructure；`MemoryIndexDispatcher` 亦直接构造 ES/Qdrant/TEI 客户端（dispatcher.py:5-14、163-178）。这是 Ports-and-Adapters 分层上的已知违规/待迁移点，而非数据权威性错误。

## 推断/未覆盖

- 推断：`agent_memory` 是 Long-term Memory 聚合的 authoritative store；ES/Qdrant 是可重建 projection，outbox 是收敛契约。依据 search.py:114-217 与 dispatcher.py:52-69、138-153 的显式注释和流程。
- 未覆盖：本报告未审计 `MemoryService` 的全部 HTTP 路由实现、数据库 schema 的完整 FK/索引定义、以及 worker 启动调度器的 cron 入口；如需验证端到端运行时频率，应继续检查 `application.py` 生命周期与 memory worker 注册。
