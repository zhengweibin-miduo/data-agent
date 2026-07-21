# Agent 对话持久化与跨会话长期记忆实施计划

## 1. 契约与数据库

- [ ] 新增 conversation Pydantic 契约：会话、文本消息、轮次、历史游标、
  有界上下文、提炼候选和响应。
- [ ] 新增 `agent_conversation`、`agent_message`、
  `conversation_memory_outbox` SQLAlchemy Core 表。
- [ ] 扩展 `agent_memory` 的 `user_id`、对话/消息来源和 purge 标志，
  保持 DDL 行兼容。
- [ ] 更新 `docs/docker/mysql/data_agent.sql` 供空环境创建完整结构；
  该目录不添加字段更新或已初始化环境升级 SQL。
- [ ] 增加配置模型和默认值，保持 `extra=forbid` 与中文字段说明。

## 2. 会话与消息主链路

- [ ] 实现 tenant-filtered ConversationRepository，不在仓储中提交 Session。
- [ ] 实现创建/列表/删除会话和 keyset 消息历史。
- [ ] 实现 `start_turn`：锁定会话、门禁并发、幂等写用户消息、返回有界上下文。
- [ ] 实现 `complete_turn`：幂等写助手消息、原子写提炼 outbox、清除在途轮次。
- [ ] 实现稳定业务错误码，不记录或返回消息、摘要和内部异常文本。
- [ ] 将 conversation router/service 接入 FastAPI composition root。

## 3. 用户长期记忆扩展

- [ ] 增加 `USER_MEMORY` 类型、category/key/value/证据契约和确定性文本/hash/scope。
- [ ] 扩展 MemoryCandidate/Detail/Projection 的用户与对话来源字段。
- [ ] 让 MemoryRepository 的用户记忆读写、历史、更新、删除和批量回查在 SQL
  中强制过滤 `user_id`。
- [ ] 保持 metadata memory API 仅访问 DDL kinds/无用户行；增加用户作用域
  search/get/history/update/delete 路由并复用同一权威服务。
- [ ] 实现同 key 新值 supersede、相同内容幂等和用户直接修正。

## 4. 异步提炼与摘要

- [ ] 实现 outbox claim lease/token、过期重领、成功确认和指数退避。
- [ ] 实现结构化 ConversationMemoryExtractor，复用现有 ChatOpenAI 客户端。
- [ ] 在代码中校验 user_id、会话/消息归属、角色、精确用户 quote 和明确确认。
- [ ] 用一次模型输出更新有界摘要并生成零到多条长期记忆候选。
- [ ] 在 finalize 事务中 compare-and-set lease、单调更新摘要、写权威记忆和
  ES/Qdrant index outbox。
- [ ] 把提炼 dispatcher 和用户记忆 purge 接入现有 arq WorkerSettings，
  不新增队列或外部客户端。

## 5. 检索、投影与重建

- [ ] 给 ES mapping/document 和 Qdrant payload/index 增加 `user_id` 与对话来源。
- [ ] 扩展 MemorySearchService：DDL 路径保持原作用域，用户路径强制
  `user_id + USER_MEMORY`。
- [ ] 保持 RRF、pending outbox 排除、版本/hash/status 和 MySQL 权威回查。
- [ ] 初始 `memory.projection_version` 使用 `v1`，不执行 recreate 或 cursor rebuild。
- [ ] 验证单个派生服务失败时仍能安全降级，不跨用户返回候选。

## 6. 删除

- [ ] 会话删除只级联其消息和提炼 outbox，不删除共享用户记忆。
- [ ] 用户删除立即删除会话数据、tombstone 用户记忆并写双目标 DELETE outbox。
- [ ] 索引删除确认后物理清理 `purge_requested_at` 记忆的 links、events 和权威行。
- [ ] 验证重试、崩溃和部分索引成功不会留下可召回或无法清理的数据。

## 7. 测试

- [ ] 单元测试：契约边界、上下文字符预算、证据验证、scope/hash、摘要游标。
- [ ] MySQL 集成：会话/消息/outbox 原子性、幂等、分页、tenant collision、
  lease 和删除顺序。
- [ ] API 集成：创建/列表/轮次/历史、404 隔离、409 并发与幂等冲突、
  文本边界和用户记忆管理。
- [ ] worker/fake LLM：直接用户事实、助手猜测、模糊确认、明确确认、提炼失败重试。
- [ ] ES/Qdrant/TEI 可用时验证用户过滤、索引重建和混合召回；不可用时如实记录。
- [ ] 回归 DDL job、interrupt/resume、checkpoint cleanup、Meta snapshot、
  DDL memory 和现有 API。

## 8. 质量与迁移门禁

- [ ] `uv lock --check`
- [ ] `uv run ruff check src tests`
- [ ] `uv run pyright src tests`
- [ ] `uv run python -m compileall -q src tests`
- [ ] `uv run python -m data_agent.settings`
- [ ] `uv run pytest -m "not integration"`
- [ ] 运行可用的 MySQL/Redis/ES/Qdrant/TEI 集成测试。
- [ ] `docker compose -f docs/docker/docker-compose.yml config`
- [ ] 在临时库验证空环境 bootstrap 和现有 v1 结构升级到新结构。
- [ ] `git diff --check`

## 9. Review Gate

- [ ] 使用 `trellis-check` 按 `code_review.md` 检查 PRD、设计、实现和跨层数据流。
- [ ] 根据实现更新 backend database、directory、external-service、error 和
  quality specs。
- [ ] 用户批准规划后才执行 `task.py start`；实现完成后另行取得提交/推送授权。

## 风险与回滚点

- MySQL 升级脚本是主要部署门禁，失败时停止新代码启动，不在半迁移
  状态接受对话写入。初始 `v1` 投影尚未使用，不设 rebuild 门禁。
- 任何租户过滤缺失都属于阻断发布问题；API 层事后过滤不能替代 SQL/索引条件。
- 用户删除先 tombstone、后 purge；不得为了立即物理删除而丢失派生索引重试能力。
- 不验证真实外部 LLM 时只能声明离线契约通过，不能声明生产端点兼容。
