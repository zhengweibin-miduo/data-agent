# 对话分区注释语义审查

## 审查文件清单

生产源码：

- `src/data_agent/conversation/__init__.py`
- `src/data_agent/conversation/api.py`
- `src/data_agent/conversation/extraction.py`
- `src/data_agent/conversation/models.py`
- `src/data_agent/conversation/mysql_tables.py`
- `src/data_agent/conversation/repository.py`
- `src/data_agent/conversation/service.py`

直接对应测试：

- `tests/unit/conversation/test_conversation.py`
- `tests/integration/persistence/test_conversation_repository.py`

未发现该分区之外仍属于 conversation 的 Python 文件。按任务边界未审查历史任务、工具技能、缓存及生成物。

## P0/P1 候选

无。逐项对照实现、直接调用方及测试后，未发现能由当前代码路径证明会造成重要功能错误、数据不一致、安全事件或发布阻塞的注释/Docstring 语义缺陷。

## 非阻塞维护候选

### M-1：`list` Docstring 声称按更新时间与主键排序，但实现只按主键排序

- 原文：`src/data_agent/conversation/repository.py:79-86`：`"""按更新时间与主键稳定读取用户会话。"""`
- 实现证据：`src/data_agent/conversation/repository.py:93-96` 使用 `.order_by(agent_conversation.c.id.desc())`，没有 `updated_at` 排序。
- 调用/测试证据：`src/data_agent/conversation/service.py:37-49` 仅透传 `before/limit`；`tests/unit/conversation/test_conversation.py:90-141` 只锁定路由，集成测试未断言会话排序。
- 影响：维护者按 Docstring 可能误以为会话更新后会改变列表顺序；实际游标语义是纯 `id` keyset。不会导致当前数据错误，但会误导接口契约和后续排序修改。
- 最小建议：若设计确实是稳定主键分页，将 Docstring 改为“按主键稳定读取”；若需更新时间排序，则同步实现 `(updated_at, id)` keyset 游标及模型字段，不能只改文字。

## 已核验无问题项

- `ConversationRepository.get/history/start_turn/complete_turn` 均在 SQL 中同时约束 `user_id` 与会话标识；集成测试 `tests/integration/persistence/test_conversation_repository.py:60-89` 验证跨租户不可见。
- `start_turn`/`complete_turn` 的幂等冲突、在途轮次门禁和 outbox 唯一性与实现及集成测试 `:33-58`, `:102-155` 一致。
- `finish_extraction` Docstring 所述 lease-token 校验和摘要游标单调推进与 `repository.py:480-518` 实现一致；`claim_extractions` 按会话最早 outbox 顺序领取（`repository.py:402-430`）。
- `_validated_candidates` Docstring 所述消息归属、角色、顺序、精确 quote 校验均在 `extraction.py:67-117` 实现，单元测试 `test_conversation.py:144-335` 覆盖伪造值、模糊确认、后续明确确认、租户 UID 隔离及同作用域去重。
- API、模型和服务 Docstring 对文本-only、上下文预算、删除后长期记忆 tombstone 等行为与签名/调用一致；未发现异常、事务或生命周期约束的误述。

## 验证透明度

本分区完成源码与对应测试的逐文件 Docstring/注释核对及关键字检索。未执行 Ruff、Pyright、pytest 或 MySQL/Redis 集成服务验证；这些由主审查会话统一执行。
