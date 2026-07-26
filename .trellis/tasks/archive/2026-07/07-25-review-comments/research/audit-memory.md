# 长期记忆分区审查

## 审查文件清单

生产：`src/data_agent/ddl_metadata/memory/**`（application/context.py、search.py、service.py、snapshots.py；domain/candidates.py、lifecycle.py、payloads.py、policies.py、ranking.py；indexing/dispatcher.py、elasticsearch.py、qdrant.py、rebuilder.py；mysql/index_outbox.py、repository.py、tables.py 及各 `__init__.py`）、`src/data_agent/ddl_metadata/models/memory.py`。

测试：`tests/integration/test_memory_services.py`、`tests/integration/persistence/test_memory_repository.py`、`tests/unit/ddl_metadata/memory/test_search.py`、`tests/unit/ddl_metadata/memory/domain/test_memory.py`、`tests/unit/ddl_metadata/memory/mysql/test_tables.py` 及对应 `__init__.py`。未纳入其它测试（不涉及长期记忆契约）。

## P0/P1 候选

未发现可由实现和测试证实的 P0/P1 阻塞问题。索引 outbox 的 `claim_outbox` 在同一数据库会话事务中持有 `FOR UPDATE SKIP LOCKED`，调度器在该事务内完成外部投影后确认/重试，未将“领取”误判为无锁并发缺陷。

## 非阻塞维护候选

1. **更新接口 Docstring 误述用户作用域生命周期（维护建议）**
   - 原文：`src/data_agent/ddl_metadata/memory/application/service.py:145`：`"""记录用户确认修正，要求完整 DDL 重处理后再成为活动事实。"""`。
   - 证据：同方法 `:169-175` 对 `user_id is not None` 直接调用 `_replace_memory`；只有 `:176-182` 的 `user_id is None` 分支进入 DDL `mutation_lease`，返回值在 `_replace_memory` 中由 `requires_reprocess=user_id is None`（约 `:347-468`）决定。因此用户级跨会话记忆会立即成为活动版本，`requires_reprocess=False`，并不需要完整 DDL 重处理。
   - 影响：调用方阅读 API 文档会误以为用户记忆更新仍处于待重处理状态，可能错误等待不存在的 DDL 作业；实现与集成测试（`tests/integration/persistence/test_memory_repository.py` 的用户修正场景）支持当前分支语义。
   - 最小建议：改为“记录用户确认修正；DDL 记忆需完整重处理，用户级记忆立即替换”，或分别为两种作用域说明副作用。

2. **响应模型 Docstring 仅描述待重处理，遗漏立即生效分支（维护建议）**
   - 原文：`src/data_agent/ddl_metadata/models/memory.py:316-322`：`MemoryUpdateResponse` 的 `"""待重新处理的用户修正响应。"""`，模型同时暴露 `requires_reprocess: bool`。
   - 证据：服务实现明确按 `user_id` 返回 `requires_reprocess=False/True`（`service.py:169-182`、`_replace_memory` 返回构造处）；因此响应并非总是“待重新处理”。
   - 影响：生成 API 文档或维护者据 Docstring 推断错误的生命周期；不改变运行时行为。
   - 最小建议：改为“用户确认修正响应；`requires_reprocess` 指示是否需 DDL 重处理”。

3. **删除接口 Docstring 省略用户级分支不使用来源租约（维护建议）**
   - 原文：`src/data_agent/ddl_metadata/memory/application/service.py:191`：`"""在来源租约内执行可审计软删除。"""`。
   - 证据：`service.py:200-220` 的 `user_id is not None` 分支直接开启 MySQL session 并软删除；来源 `mutation_lease` 仅在 `:221` 之后的非用户分支使用。
   - 影响：文档对并发/事务边界的描述不准确，可能让调用方误以为用户删除会与 DDL 来源租约串行化。
   - 最小建议：说明“DDL 记忆删除在来源租约内；用户级记忆在独立事务中删除”。

## 确认无问题项

- `MemoryScopeType`、`schema_fingerprint`、`active_key` 与仓储作用域过滤保持一致；集成测试覆盖同槽幂等、A→B→A 版本化、删除 tombstone 防重放及局部 DDL 指纹过期。
- 搜索服务先 MySQL 权威回查再过滤活动状态、版本、内容哈希、过期时间和 outbox 待确认目标；索引异常降级及访问统计 best-effort 均有单元测试（`tests/unit/ddl_metadata/memory/test_search.py`）。
- 只追加历史、双目标 outbox、软删除和生命周期策略 Docstring 与实现/测试一致；未发现 TODO/FIXME/HACK 等待办或注释掉的代码。

## 验证限制

本分区未运行全仓 Ruff/集成数据库命令；结论基于静态逐文件核对及现有测试源码。外部 ES/Qdrant/MySQL 运行时行为未作环境验证。
