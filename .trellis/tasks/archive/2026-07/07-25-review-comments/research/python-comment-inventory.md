# Python 注释确定性清单

由 `generate_comment_inventory.py` 基于 AST 与 tokenize 生成。

## 汇总

- `src/`：85 个文件，520 处 Docstring，62 处普通注释。
- `tests/`：47 个文件，252 处 Docstring，0 处普通注释。
- 待办标记：0 处。

## 逐文件统计

| 文件 | Docstring | 普通注释 | 待办标记 |
| --- | ---: | ---: | ---: |
| `src/data_agent/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/application.py` | 5 | 0 | 0 |
| `src/data_agent/conversation/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/conversation/api.py` | 15 | 0 | 0 |
| `src/data_agent/conversation/extraction.py` | 6 | 4 | 0 |
| `src/data_agent/conversation/models.py` | 18 | 0 | 0 |
| `src/data_agent/conversation/mysql_tables.py` | 1 | 0 | 0 |
| `src/data_agent/conversation/repository.py` | 17 | 10 | 0 |
| `src/data_agent/conversation/service.py` | 11 | 3 | 0 |
| `src/data_agent/ddl_metadata/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/api/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/api/job_events.py` | 5 | 0 | 0 |
| `src/data_agent/ddl_metadata/api/jobs.py` | 6 | 0 | 0 |
| `src/data_agent/ddl_metadata/api/memories.py` | 7 | 0 | 0 |
| `src/data_agent/ddl_metadata/api/router.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/errors.py` | 3 | 0 | 0 |
| `src/data_agent/ddl_metadata/identifiers.py` | 7 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/identifiers.py` | 2 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/base.py` | 3 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/codec.py` | 6 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/event_store.py` | 6 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/keys.py` | 10 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/lease_store.py` | 5 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/outbox_store.py` | 6 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/scripts.py` | 2 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/redis/state_store.py` | 9 | 0 | 0 |
| `src/data_agent/ddl_metadata/jobs/store.py` | 32 | 8 | 0 |
| `src/data_agent/ddl_metadata/memory/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/application/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/application/context.py` | 5 | 2 | 0 |
| `src/data_agent/ddl_metadata/memory/application/search.py` | 3 | 5 | 0 |
| `src/data_agent/ddl_metadata/memory/application/service.py` | 13 | 6 | 0 |
| `src/data_agent/ddl_metadata/memory/application/snapshots.py` | 3 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/domain/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/domain/candidates.py` | 3 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/domain/lifecycle.py` | 3 | 1 | 0 |
| `src/data_agent/ddl_metadata/memory/domain/payloads.py` | 6 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/domain/policies.py` | 5 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/domain/ranking.py` | 2 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/indexing/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/indexing/dispatcher.py` | 3 | 1 | 0 |
| `src/data_agent/ddl_metadata/memory/indexing/elasticsearch.py` | 8 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/indexing/qdrant.py` | 10 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/indexing/rebuilder.py` | 4 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/mysql/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/mysql/index_outbox.py` | 11 | 1 | 0 |
| `src/data_agent/ddl_metadata/memory/mysql/repository.py` | 27 | 0 | 0 |
| `src/data_agent/ddl_metadata/memory/mysql/tables.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/models/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/models/base.py` | 2 | 0 | 0 |
| `src/data_agent/ddl_metadata/models/jobs.py` | 12 | 0 | 0 |
| `src/data_agent/ddl_metadata/models/memory.py` | 31 | 0 | 0 |
| `src/data_agent/ddl_metadata/models/physical.py` | 4 | 0 | 0 |
| `src/data_agent/ddl_metadata/models/semantic.py` | 12 | 0 | 0 |
| `src/data_agent/ddl_metadata/parsing.py` | 9 | 0 | 0 |
| `src/data_agent/ddl_metadata/persistence/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/persistence/metadata_repository.py` | 7 | 0 | 0 |
| `src/data_agent/ddl_metadata/persistence/schema.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/persistence/tables.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/validation.py` | 5 | 0 | 0 |
| `src/data_agent/ddl_metadata/worker/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/worker/job_runner.py` | 8 | 8 | 0 |
| `src/data_agent/ddl_metadata/worker/lifecycle.py` | 3 | 0 | 0 |
| `src/data_agent/ddl_metadata/worker/maintenance.py` | 8 | 2 | 0 |
| `src/data_agent/ddl_metadata/worker/settings.py` | 2 | 0 | 0 |
| `src/data_agent/ddl_metadata/workflow/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/ddl_metadata/workflow/contracts.py` | 10 | 0 | 0 |
| `src/data_agent/ddl_metadata/workflow/graph.py` | 2 | 2 | 0 |
| `src/data_agent/ddl_metadata/workflow/llm_metadata_generator.py` | 7 | 0 | 0 |
| `src/data_agent/ddl_metadata/workflow/nodes.py` | 15 | 8 | 0 |
| `src/data_agent/ddl_metadata/workflow/routing.py` | 8 | 0 | 0 |
| `src/data_agent/ddl_metadata/workflow/state.py` | 2 | 0 | 0 |
| `src/data_agent/infrastructure/__init__.py` | 1 | 0 | 0 |
| `src/data_agent/infrastructure/checkpoint_store.py` | 5 | 0 | 0 |
| `src/data_agent/infrastructure/elasticsearch.py` | 5 | 0 | 0 |
| `src/data_agent/infrastructure/llm_client.py` | 7 | 0 | 0 |
| `src/data_agent/infrastructure/mysql.py` | 6 | 0 | 0 |
| `src/data_agent/infrastructure/qdrant.py` | 5 | 0 | 0 |
| `src/data_agent/infrastructure/redis.py` | 5 | 0 | 0 |
| `src/data_agent/infrastructure/tei_embeddings.py` | 7 | 0 | 0 |
| `src/data_agent/logging.py` | 5 | 0 | 0 |
| `src/data_agent/main.py` | 2 | 0 | 0 |
| `src/data_agent/settings.py` | 18 | 1 | 0 |
| `tests/__init__.py` | 1 | 0 | 0 |
| `tests/helpers/__init__.py` | 1 | 0 | 0 |
| `tests/helpers/checks.py` | 7 | 0 | 0 |
| `tests/helpers/factories.py` | 5 | 0 | 0 |
| `tests/helpers/fakes.py` | 10 | 0 | 0 |
| `tests/integration/__init__.py` | 1 | 0 | 0 |
| `tests/integration/infrastructure/__init__.py` | 1 | 0 | 0 |
| `tests/integration/infrastructure/test_mysql.py` | 7 | 0 | 0 |
| `tests/integration/infrastructure/test_redis.py` | 3 | 0 | 0 |
| `tests/integration/infrastructure/test_tei_embeddings.py` | 2 | 0 | 0 |
| `tests/integration/persistence/__init__.py` | 1 | 0 | 0 |
| `tests/integration/persistence/test_conversation_repository.py` | 3 | 0 | 0 |
| `tests/integration/persistence/test_memory_repository.py` | 9 | 0 | 0 |
| `tests/integration/persistence/test_metadata_repository.py` | 5 | 0 | 0 |
| `tests/integration/test_api.py` | 6 | 0 | 0 |
| `tests/integration/test_ddl_metadata_flow.py` | 4 | 0 | 0 |
| `tests/integration/test_job_events.py` | 3 | 0 | 0 |
| `tests/integration/test_memory_services.py` | 2 | 0 | 0 |
| `tests/integration/test_worker.py` | 9 | 0 | 0 |
| `tests/unit/__init__.py` | 1 | 0 | 0 |
| `tests/unit/conversation/__init__.py` | 1 | 0 | 0 |
| `tests/unit/conversation/test_conversation.py` | 13 | 0 | 0 |
| `tests/unit/ddl_metadata/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/jobs/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/jobs/redis/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/jobs/redis/test_event_store.py` | 10 | 0 | 0 |
| `tests/unit/ddl_metadata/jobs/redis/test_job_stores.py` | 7 | 0 | 0 |
| `tests/unit/ddl_metadata/memory/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/memory/domain/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/memory/domain/test_memory.py` | 6 | 0 | 0 |
| `tests/unit/ddl_metadata/memory/mysql/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/memory/mysql/test_tables.py` | 3 | 0 | 0 |
| `tests/unit/ddl_metadata/memory/test_search.py` | 27 | 0 | 0 |
| `tests/unit/ddl_metadata/test_job_events.py` | 20 | 0 | 0 |
| `tests/unit/ddl_metadata/test_job_events_api.py` | 16 | 0 | 0 |
| `tests/unit/ddl_metadata/test_package_contracts.py` | 4 | 0 | 0 |
| `tests/unit/ddl_metadata/test_parsing.py` | 5 | 0 | 0 |
| `tests/unit/ddl_metadata/test_validation.py` | 3 | 0 | 0 |
| `tests/unit/ddl_metadata/worker/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/worker/test_job_runner.py` | 5 | 0 | 0 |
| `tests/unit/ddl_metadata/workflow/__init__.py` | 1 | 0 | 0 |
| `tests/unit/ddl_metadata/workflow/test_graph.py` | 6 | 0 | 0 |
| `tests/unit/infrastructure/__init__.py` | 1 | 0 | 0 |
| `tests/unit/infrastructure/test_llm_client.py` | 3 | 0 | 0 |
| `tests/unit/infrastructure/test_logging.py` | 12 | 0 | 0 |
| `tests/unit/infrastructure/test_logging_lifecycle.py` | 16 | 0 | 0 |
| `tests/unit/test_settings.py` | 5 | 0 | 0 |

## 普通注释明细

- `src/data_agent/conversation/extraction.py:64`：`# 模型输出不具备权威性；只有能回查到本批原始消息、角色和顺序的精确用户`
- `src/data_agent/conversation/extraction.py:65`：`# quote（以及对助手结论的明确后续确认）才允许写入长期记忆。`
- `src/data_agent/conversation/extraction.py:197`：`# 模型调用不占用数据库事务；成功时候选记忆与摘要确认同事务提交，`
- `src/data_agent/conversation/extraction.py:198`：`# 任一步失败都保留 outbox，并释放 lease 后退避重试。`
- `src/data_agent/conversation/repository.py:169`：`# 递减主键游标不受新消息插入影响；多取一条判定续页后再恢复时间线正序。`
- `src/data_agent/conversation/repository.py:196`：`# 先锁定会话再检查 active_turn_uid，避免并发请求同时通过门禁；`
- `src/data_agent/conversation/repository.py:197`：`# 同一 turn 仅允许相同内容幂等重试，内容变化必须拒绝。`
- `src/data_agent/conversation/repository.py:322`：`# 助手消息、outbox 与清除 active_turn_uid 必须在调用方的同一事务中成败一致，`
- `src/data_agent/conversation/repository.py:323`：`# 任一步失败都不能提前放行下一轮。`
- `src/data_agent/conversation/repository.py:387`：`# 摘要游标与 through_id 圈定可见区间；先保留最新窗口，再恢复时间线正序。`
- `src/data_agent/conversation/repository.py:408`：`# 每个会话只领取最早未完成轮次以顺序推进摘要；过期租约可重领，`
- `src/data_agent/conversation/repository.py:409`：`# skip_locked 则允许其他 worker 继续处理不同会话。`
- `src/data_agent/conversation/repository.py:494`：`# 完成时复核 lease token，阻止任务被重新领取后的旧 worker 写入；`
- `src/data_agent/conversation/repository.py:495`：`# 摘要更新与 outbox 删除在同一事务提交，且游标只能前进。`
- `src/data_agent/conversation/service.py:140`：`# 先清除会话及其 outbox，再 tombstone 长期记忆；两步共享事务，`
- `src/data_agent/conversation/service.py:141`：`# 避免删除请求留下仍可检索的孤立记忆。`
- `src/data_agent/conversation/service.py:177`：`# 字符预算从最新消息向前保留，再反转回时间线顺序，确保新上下文优先。`
- `src/data_agent/ddl_metadata/jobs/store.py:91`：`# 任务、来源租约和 dispatch outbox 共同构成受理边界；后续公开事件只是`
- `src/data_agent/ddl_metadata/jobs/store.py:92`：`# 可由权威 Hash 修复的通知，发布失败不能撤销已经持久化的受理结果。`
- `src/data_agent/ddl_metadata/jobs/store.py:171`：`# 状态表限制合法边，revision 则充当并发 worker 的 CAS 门闩；只有原子`
- `src/data_agent/ddl_metadata/jobs/store.py:172`：`# 转换胜出的调用方才重新读取并发布当前投影，旧执行不能覆盖权威状态。`
- `src/data_agent/ddl_metadata/jobs/store.py:233`：`# 当前问题 ID 先做业务校验，revision、question_set_id、截止时间和载荷`
- `src/data_agent/ddl_metadata/jobs/store.py:234`：`# 哈希再由 Redis 脚本原子裁决，使重复提交幂等而过期轮次不能恢复图。`
- `src/data_agent/ddl_metadata/jobs/store.py:302`：`# 转换成功时，终态、来源租约释放、结果保留和 checkpoint 清理 outbox`
- `src/data_agent/ddl_metadata/jobs/store.py:303`：`# 原子生效；线程删除由可重放维护任务完成，不依赖当前 worker。`
- `src/data_agent/ddl_metadata/memory/application/context.py:53`：`# 用户确认只提高可信优先级；同作用域的不同确认内容仍必须显式报冲突。`
- `src/data_agent/ddl_metadata/memory/application/context.py:81`：`# 指纹与 content hash 命中仅产生候选，当前 DDL AST 校验始终拥有最终裁决权。`
- `src/data_agent/ddl_metadata/memory/application/search.py:57`：`# 派生索引只贡献候选和排名信号，MySQL exact 与后续回查始终保留权威性。`
- `src/data_agent/ddl_metadata/memory/application/search.py:141`：`# 待投影目标可能仍指向旧版本，必须先剔除其信号再执行 RRF 融合。`
- `src/data_agent/ddl_metadata/memory/application/search.py:158`：`# RRF 分数不能绕过版本、hash、过期时间，以及调用方提供的对象白名单。`
- `src/data_agent/ddl_metadata/memory/application/search.py:198`：`# 访问热度不影响本次结果正确性，统计失败不得撤销已完成的权威过滤。`
- `src/data_agent/ddl_metadata/memory/application/search.py:207`：`# noqa: BLE001`
- `src/data_agent/ddl_metadata/memory/application/service.py:169`：`# 两类修正都立即创建活动记忆版本；DDL 修正额外与同来源工作流串行，`
- `src/data_agent/ddl_metadata/memory/application/service.py:170`：`# 并用 requires_reprocess 提示调用方重新生成 Meta。`
- `src/data_agent/ddl_metadata/memory/application/service.py:202`：`# 删除保留历史并投递索引 DELETE，而不物理移除权威记录；来源租约仅用于`
- `src/data_agent/ddl_metadata/memory/application/service.py:203`：`# 防止 DDL 工作流并发重建覆盖删除。`
- `src/data_agent/ddl_metadata/memory/application/service.py:275`：`# 修正只能改变事实内容，原类别、scope key、对象身份与证据归属均不可迁移。`
- `src/data_agent/ddl_metadata/memory/application/service.py:400`：`# 入口校验不能替代锁内复核；新版本、历史事件与活动槽位必须原子提交。`
- `src/data_agent/ddl_metadata/memory/domain/lifecycle.py:32`：`# 先裁决删除、空槽位和幂等分支，避免 tombstone 或同内容被误判为新增版本。`
- `src/data_agent/ddl_metadata/memory/indexing/dispatcher.py:32`：`# ES/Qdrant 独立确认；单目标失败只退避自身行，不得阻断或代替另一目标。`
- `src/data_agent/ddl_metadata/memory/mysql/index_outbox.py:110`：`# 领取锁只覆盖当前事务；完整期望条件确保迟到 worker 无法删除更新后的状态。`
- `src/data_agent/ddl_metadata/worker/job_runner.py:261`：`# 任务绑定的 graph_version 不允许由新图继续解释；PENDING 任务先通过`
- `src/data_agent/ddl_metadata/worker/job_runner.py:262`：`# 修订保护进入 RUNNING，再按统一终态路径记录 attempt 并安排清理。`
- `src/data_agent/ddl_metadata/worker/job_runner.py:352`：`# 同一 job_id 始终绑定同一 checkpoint 线程：无快照才注入原始请求，`
- `src/data_agent/ddl_metadata/worker/job_runner.py:353`：`# interrupt 只恢复已提交回答，已完成快照先投影终态，其余情况续跑现有图。`
- `src/data_agent/ddl_metadata/worker/job_runner.py:392`：`# sync durability 先固化节点结果再让 worker 继续；tasks 流只映射稳定阶段，`
- `src/data_agent/ddl_metadata/worker/job_runner.py:393`：`# 节点输入、输出、interrupt 和错误均不得进入公开进度事件。`
- `src/data_agent/ddl_metadata/worker/job_runner.py:431`：`# 仅显式瞬态异常可在预算内回到 PENDING；指数退避与 checkpoint 复用`
- `src/data_agent/ddl_metadata/worker/job_runner.py:432`：`# 共同避免热循环，也避免持久化重试重复已经完成的模型节点。`
- `src/data_agent/ddl_metadata/worker/maintenance.py:38`：`# 终态转换只写清理 outbox；线程删除成功后才确认，确保 worker 崩溃或 Redis`
- `src/data_agent/ddl_metadata/worker/maintenance.py:39`：`# 短暂失败时维护任务仍能重放，而不会静默遗留或提前丢失 checkpoint。`
- `src/data_agent/ddl_metadata/workflow/graph.py:38`：`# 路由把模型输出夹在确定性校验之间，route 只由当前节点写入；只有完成语义、`
- `src/data_agent/ddl_metadata/workflow/graph.py:39`：`# 问答和指标校验的 finalized 数据才能构建记忆并抵达唯一持久化出口。`
- `src/data_agent/ddl_metadata/workflow/nodes.py:163`：`# semantic_attempts 存在 checkpoint 中，并由结构化解析与确定性校验`
- `src/data_agent/ddl_metadata/workflow/nodes.py:164`：`# 共享一次修复预算；再次失败必须转业务拒绝，不能形成无限模型回环。`
- `src/data_agent/ddl_metadata/workflow/nodes.py:292`：`# question_set_id 与 revision 共同锚定外部回答，round 仅说明当前轮次；`
- `src/data_agent/ddl_metadata/workflow/nodes.py:293`：`# 恢复后按 question_id 合并历史证据，陈旧提交由 store 的 CAS 拒绝。`
- `src/data_agent/ddl_metadata/workflow/nodes.py:351`：`# metric_attempts 同样随 checkpoint 恢复且只允许一次可修复回环；`
- `src/data_agent/ddl_metadata/workflow/nodes.py:352`：`# 持续无效的模型输出必须收敛为结构化拒绝，而不是重复消耗模型调用。`
- `src/data_agent/ddl_metadata/workflow/nodes.py:507`：`# 记忆候选只来自已 finalized 的语义和指标；snapshot 事务成功后才返回`
- `src/data_agent/ddl_metadata/workflow/nodes.py:508`：`# SUCCEEDED，异常则交由 runner 统一分类，避免出现成功投影但快照未提交。`
- `src/data_agent/settings.py:341`：`# 仅加载一次，供所有应用模块共享。`

## 待办标记明细

- 未发现。
