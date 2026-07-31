# AI 聊天能力缺口与最小接入点

## 当前链路

- `src/data_agent/conversation/api.py:94-109`：`start_turn` 只接收并保存用户消息，返回有界上下文。
- `src/data_agent/conversation/service.py:104-130`：上下文包含摘要、近期消息和同一用户的长期记忆，但不会调用 LLM。
- `src/data_agent/conversation/api.py:112-128`：`complete_turn` 要求调用方直接提供助手文本。
- `src/data_agent/conversation/service.py:132-148`：`complete_turn` 只保存助手消息并安排记忆提炼 outbox。
- `src/data_agent/infrastructure/llm_client.py:18-47`：已有服务端共享 `ChatOpenAI` 客户端和环境变量密钥边界，可供编排层复用。
- `src/data_agent/ddl_metadata/workflow/graph.py:32-96`：现有 LangGraph 是 DDL worker 图，不是通用会话运行时，不应整图复用到聊天。

因此现有前端若调用 Conversation API，只能自己伪造助手文本或直接持有模型密钥，均不可接受。

## 最小后端扩展

新增独立的 chat-turn application orchestration 与 HTTP 端点，内部顺序为：

1. 校验 conversation/user/turn 幂等坐标。
2. 调用现有 `ConversationService.start_turn` 保存用户消息并取得有界上下文。
3. 对数据问题调用 `AnswerReadinessService.evaluate`；未就绪返回安全固定消息。
4. 将系统提示、摘要、近期消息、长期记忆和当前问题交给服务端 `LLMClient`。
5. 获得助手内容后调用 `ConversationService.complete_turn` 持久化，并复用现有记忆提炼 outbox。
6. 返回助手 `MessageRecord` 与安全的 readiness 状态；是否需要 token streaming 在产品目标确认后决定，不预先引入 WebSocket。

## 与 DDL 澄清的边界

- `src/data_agent/ddl_metadata/api/jobs.py:57-65`：SSE 不暴露 LangGraph interrupt 载荷。
- `src/data_agent/ddl_metadata/api/jobs.py:85-102`：DDL 回答必须由 job 门面按 `question_set_id`、`revision` 和截止时间校验。
- AI 可以根据当前 DDL、会话上下文和澄清问题解释含义、起草回答。
- 只有用户点击“确认并继续”后，前端才调用 job answers API；普通聊天消息不能自动推进 DDL 工作流。
- 用户长期记忆与 DDL 元数据记忆保持现有数据库边界，不把聊天历史直接写成权威元数据。
