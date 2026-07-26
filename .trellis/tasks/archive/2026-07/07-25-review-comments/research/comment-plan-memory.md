# 长期记忆核心流程注释定位清单

范围依据：`prd.md` R9/R10、`design.md` 长期记忆分区、`.trellis/spec/backend/quality-guidelines.md` Docstring/inline comment contract。以下仅列解释顺序、原因、边界或不变量的候选点，不建议逐行翻译。

## 建议插入点（12）

1. **用户级/DDL 级更新分支** — `src/data_agent/ddl_metadata/memory/application/service.py:137-182`，`MemoryService.update`。`user_id is not None` 直接 `_replace_memory`，用户级记忆立即成为活动事实；DDL 级必须持有 `self._jobs.mutation_lease(source)`，并由响应 `requires_reprocess` 区分生命周期。建议中文注释：`用户级修正独立事务立即替换；DDL 级修正必须在来源租约内写入，等待完整重处理后再成为活动事实，两个分支不能混用租约语义。` 证据：同文件 `:169-182`、`:_replace_memory:395-435`；现有集成持久化测试覆盖用户修正与 DDL 重处理响应。

2. **删除的软删除与租约边界** — `service.py:184-242`，`MemoryService.delete`。用户级分支在独立 MySQL 事务中 `for_update` 后软删除；DDL 分支先获取来源租约再锁行，防止工作流并发重建覆盖删除。建议：`删除只写 tombstone 并保留历史；DDL 删除与来源工作流串行化，用户级删除不占用来源租约。` 证据：`:200-242`；`tests/integration/persistence/test_memory_repository.py` 覆盖 tombstone/删除防重放。

3. **版本化替换与双重乐观并发检查** — `service.py:395-435`，`MemoryService._replace_memory`。在事务内重新 `for_update` 检查 ACTIVE 和 `record_version == expected_version`，再 upsert 新候选并追溯历史；返回版本递增和 `requires_reprocess`。建议：`入口检查不能替代事务内锁定复核；历史追加和新活动版本必须在同一事务中完成，避免并发更新产生双活动事实。` 证据：`:395-415`、`:423-435`。

4. **作用域/身份不可跨越的内容归一化** — `service.py:265-337`，`_normalize_content`。更新强制保留原内容类型、对象 ID、事实表/指标名称和证据字段，阻止用户修正把记忆移到另一对象或类别。建议：`修正只改变允许的事实字段；scope key、对象身份和证据归属是不可变边界。` 证据：`:271-303`、`:318-336`；对应单元测试检查 category/scope conflict。

5. **混合检索的权威回查顺序** — `src/data_agent/ddl_metadata/memory/application/search.py:57-155`，`MemorySearchService.search`。先 MySQL exact baseline，并发 ES/Qdrant；合并前读取活动记录和 outbox pending targets，待确认目标不得参与派生排名，随后 RRF。建议：`索引只提供候选和排名信号，MySQL 是权威状态；必须先排除待投影目标再融合，避免读到旧版本或已删除事实。` 证据：`:57-69`、`:117-155`。

6. **检索结果的最终安全过滤** — `search.py:156-193`。再次校验 source/user scope、ACTIVE、content/projection version、content hash、过期时间和 allowed object IDs，再按类别权重和重要性排序。建议：`RRF 分数不能绕过权威过滤；版本/hash/过期和 DDL 对象白名单共同构成结果可用性门槛。` 证据：`:156-193`；`tests/unit/ddl_metadata/memory/test_search.py` 覆盖 stale、soft-delete、scope 和 expiry。

7. **访问统计是 best-effort 副作用** — `search.py:195-212`。结果已经确定后单独事务写 `record_access`；统计失败仅记录 warning，不影响已过滤的搜索响应。建议：`访问热度不参与本次结果正确性；统计写入失败不得回滚或清空已生成的权威结果。` 证据：`:195-211`、`MemoryRepository.record_access` `repository.py:941`。

8. **DDL 上下文作用域指纹与最终裁决** — `src/data_agent/ddl_metadata/memory/application/context.py:75-166`，`MemoryContextLoader.load`。按 table/column `scope_fingerprint` 批量找兼容记忆，校验 content hash；混合搜索仅补缺失作用域；选中的 capsule 必须再次通过当前 AST `validate_metadata`，不完整/冲突则放弃复用。建议：`缓存命中只是候选；当前 DDL AST 校验拥有最终裁决权，指纹和 content hash 任一不匹配都不能复用。` 证据：`:80-114`、`:127-166`。

9. **用户确认优先与同作用域冲突拒绝** — `context.py:48-69`，`_choose_memory`。优先 `trust == user_confirmed`；同作用域存在多个不同 canonical payload 时抛出 `memory_conflict`，不能静默择一。建议：`用户确认优先级只解决可信度，不掩盖同作用域的内容冲突；冲突必须显式失败等待重处理。` 证据：`:52-69`；集成测试覆盖冲突路径。

10. **确定性生命周期决策** — `src/data_agent/ddl_metadata/memory/domain/lifecycle.py:24-40`，`decide_memory`。删除、无活动、同内容、merge、update 顺序是业务不变量，模型比较前先处理可确定分支。建议：`先处理删除和无活动等确定分支，再进入 merge/update；顺序改变会把 tombstone 或幂等 NOOP 误判为新增。` 证据：`:31-40`；memory domain tests 覆盖五类决策。

11. **双目标 outbox 的行锁领取与确认条件** — `src/data_agent/ddl_metadata/memory/mysql/index_outbox.py:86-117`，`claim_outbox`/`acknowledge_outbox`。`FOR UPDATE SKIP LOCKED` 在当前 DB session 事务中避免 worker 互相领取；确认删除必须同时匹配 uid、target、operation、projection_version，防止旧 worker 删除新期望状态。建议：`领取锁只保护当前事务；ack 必须带完整期望版本条件，过期 worker 只能成为 no-op。` 证据：`:86-117`；`tests/integration/persistence/test_memory_repository.py` 覆盖 outbox 幂等。

12. **索引投影重试/双目标分发** — `src/data_agent/ddl_metadata/memory/indexing/dispatcher.py:26-66`，`MemoryIndexDispatcher.dispatch`。每批在同一 session 有界领取，按 target 分别执行 ES/Qdrant（Qdrant 需 embedding），成功才 ack；异常按指数退避更新 attempts/available_at，另一目标仍可独立处理。建议：`ES 与 Qdrant 是独立投影目标；单目标失败只延迟该 outbox 行，不得确认或阻断另一目标，重试必须保持幂等。` 证据：`:29-65`、`index_outbox.py:119-140`。

## 三处文案修复（与既有审查证据一致）

- `service.py:145`：将“记录用户确认修正，要求完整 DDL 重处理后再成为活动事实。”改为“记录用户确认修正；DDL 记忆需完整重处理，用户级记忆立即替换”。
- `service.py:191`：将“在来源租约内执行可审计软删除。”改为“执行可审计软删除；DDL 记忆在来源租约内删除，用户级记忆在独立事务中删除”。
- `models/memory.py:316-322` `MemoryUpdateResponse`：将“待重新处理的用户修正响应。”改为“用户确认修正响应；`requires_reprocess` 指示是否需 DDL 重处理”。

## 不应新增注释的机械映射

- `MemoryService.get/history` 的简单 session→repository→return 流程（`service.py:91-135`）。
- `MemorySearchService` 中 ES/Qdrant 客户端构造、参数逐项转发和 `items.sort` 等可由语句直接读出的机械代码。
- `MemoryRepository` 的字段 select/row mapping、简单 `record_access` SQL 更新、模型 `Field`/Enum 定义。
- `MemoryIndexDispatcher` 中 target 分支内的单纯 client 初始化和参数传递；应只注释跨目标独立性与 ack/retry 不变量。
- `domain/payloads.py` 的 canonical JSON/hash 逐字段映射、各 `__init__.py` re-export。

## 现有证据与限制

`research/audit-memory.md` 已确认无 P0/P1；现有测试覆盖作用域、A→B→A 版本化、软删除、搜索权威回查、RRF、索引异常降级和访问统计。以上定位来自静态代码与测试源码，未运行外部 ES/Qdrant/MySQL 环境。
