# Agent 对话持久化与跨会话长期记忆设计

## 1. 设计边界

本任务建设 Agent 对话的持久化与记忆后端，不实现通用聊天 Agent 或 UI。
未来 Agent 运行时负责调用模型生成助手回复；本服务负责：

1. 持久化用户消息并返回有界上下文；
2. 持久化助手回复并把轮次标记为完成；
3. 异步更新会话摘要并提炼用户长期记忆；
4. 提供会话历史和同用户长期记忆管理。

Redis/LangGraph checkpoint 保持 DDL 工作流专用。所有对话权威数据写入
MySQL `data_agent`，ES/Qdrant 继续只是长期记忆的可重建投影。

## 2. 模块布局

新增单一领域包，避免移动已经稳定的 DDL 模块：

```text
src/data_agent/conversation/
├── __init__.py
├── api.py
├── models.py
├── service.py
├── repository.py
├── mysql_tables.py
└── extraction.py
```

- `api.py`：HTTP 参数、响应和服务委托。
- `models.py`：会话、消息、历史、上下文、提炼结果契约。
- `service.py`：会话、轮次、上下文、删除和事务边界。
- `repository.py`：带 `user_id` 条件的 SQLAlchemy Core 查询与 outbox lease。
- `mysql_tables.py`：会话、消息和提炼 outbox 表。
- `extraction.py`：结构化提炼、证据校验、摘要更新和记忆候选转换。

现有 `ddl_metadata.memory` 继续拥有权威长期记忆、事件、链接、搜索、
ES/Qdrant 投影和索引 outbox。首版只扩展它，不做包搬迁。

## 3. MySQL 数据模型

### 3.1 `agent_conversation`

```text
id                          BIGINT AUTO_INCREMENT PRIMARY KEY
uid                         CHAR(64) UNIQUE
user_id                     VARCHAR(128)
summary                     TEXT NULL
summary_through_message_id  BIGINT NULL
active_turn_uid             CHAR(64) NULL
created_at                  DATETIME
updated_at                  DATETIME
INDEX (user_id, updated_at, id)
```

`active_turn_uid` 是首版单会话并发门禁。相同轮次可以重试，不同轮次在已有
在途轮次时返回 `409 conversation_busy`。

### 3.2 `agent_message`

```text
id               BIGINT AUTO_INCREMENT PRIMARY KEY
uid              CHAR(64) UNIQUE
user_id          VARCHAR(128)
conversation_id  BIGINT
turn_uid         CHAR(64)
role             VARCHAR(16)  # user / assistant
content          TEXT
created_at       DATETIME
UNIQUE (conversation_id, turn_uid, role)
INDEX (user_id, conversation_id, id)
FOREIGN KEY (conversation_id) REFERENCES agent_conversation(id) ON DELETE CASCADE
```

冗余 `user_id` 让每条消息查询显式包含租户条件。仓储在同一事务中校验它与
父会话一致。自增 `id` 同时是稳定时间顺序和 keyset pagination 游标。

### 3.3 `conversation_memory_outbox`

```text
id                    BIGINT AUTO_INCREMENT PRIMARY KEY
user_id               VARCHAR(128)
conversation_id       BIGINT
turn_uid              CHAR(64)
user_message_id       BIGINT
assistant_message_id  BIGINT
attempts              INT
available_at          DATETIME
lease_token           CHAR(32) NULL
lease_expires_at      DATETIME NULL
last_error_type       VARCHAR(128) NULL
created_at            DATETIME
updated_at            DATETIME
UNIQUE (conversation_id, turn_uid)
INDEX (available_at, lease_expires_at, id)
FOREIGN KEY (conversation_id) REFERENCES agent_conversation(id) ON DELETE CASCADE
```

完成助手消息的事务同时写入 outbox。worker 使用短事务写入 lease/token，
提交后才调用 LLM，最后在新事务中按 token compare-and-set 完成或记录重试，
不在外部模型调用期间持有数据库行锁。

### 3.4 扩展 `agent_memory`

现有权威表增加可空字段：

```text
user_id                   VARCHAR(128) NULL
created_conversation_uid  CHAR(64) NULL
created_message_uid       CHAR(64) NULL
purge_requested_at        DATETIME NULL
INDEX (user_id, kind, status, updated_at)
```

- DDL 记忆保持 `user_id IS NULL`，现有 `source` 和 `created_job_id` 不变。
- 对话记忆使用固定 `source=data_agent_conversation`、非空 `user_id`，
  `created_job_id=NULL`，并保存会话/消息来源。
- 对话记忆 UID、唯一性和所有读取都包含 `user_id`，不能只在服务返回前过滤。
- `purge_requested_at` 只用于用户级清除；普通记忆软删除继续保留审计历史。

现有 `created_job_id` 列改为可空。新环境 bootstrap 和一次性升级 SQL 必须
同时更新；不引入 Alembic。

## 4. HTTP 与服务契约

### 4.1 会话

```http
POST   /api/v1/conversations
GET    /api/v1/conversations?user_id=...&before=...&limit=...
DELETE /api/v1/conversations/{conversation_uid}?user_id=...
GET    /api/v1/conversations/{conversation_uid}/messages?user_id=...&before=...&limit=...
```

创建请求包含 `user_id`。列表与消息历史使用 keyset 游标，返回数据按时间
正序展示。不存在和不属于该用户统一返回 `404`，避免泄露对象存在性。

### 4.2 轮次

```http
POST /api/v1/conversations/{conversation_uid}/turns
POST /api/v1/conversations/{conversation_uid}/turns/{turn_uid}/assistant
```

开始轮次请求包含 `user_id`、稳定 `turn_uid` 和用户文本，响应包含已持久化
用户消息及有界上下文。未来 Agent 运行时使用该上下文调用模型。

完成轮次请求包含相同 `user_id` 和助手文本。服务在一个事务中插入助手消息、
清除 `active_turn_uid`、更新会话时间并写入提炼 outbox。重复完成返回已存在
结果；同一标识但内容不同返回 `409 idempotency_conflict`。

首版没有附件、工具事件、流式 token 存储或由本服务直接生成助手回复。

### 4.3 用户长期记忆

```http
GET    /api/v1/users/{user_id}/memories/search
GET    /api/v1/users/{user_id}/memories/{memory_uid}
GET    /api/v1/users/{user_id}/memories/{memory_uid}/history
PATCH  /api/v1/users/{user_id}/memories/{memory_uid}
DELETE /api/v1/users/{user_id}/memories/{memory_uid}
DELETE /api/v1/users/{user_id}/conversation-data
```

这些路由复用现有 MemoryService/Repository 行为并强制 `user_id`。现有
`/api/v1/metadata/memories/**` 只允许 DDL 记忆类型和 `user_id IS NULL`，
防止通过旧路由访问对话记忆。

用户直接修正对话记忆时，保留 category/key，创建 `user_confirmed` 新内容并
通过 `SUPERSEDES` 替代旧内容；不要求重跑 DDL。

## 5. 轮次与上下文数据流

```text
future Agent runtime
  -> start turn
  -> MySQL: user message + active_turn_uid
  -> ContextAssembler:
       conversation summary
       bounded messages after summary cursor
       MemorySearchService(user_id, current input)
       current input
  -> future Agent runtime invokes model
  -> complete turn
  -> MySQL transaction:
       assistant message
       conversation_memory_outbox
       clear active_turn_uid
  -> return completed turn
```

上下文读取使用配置的消息条数、总字符数、摘要长度和长期记忆数量上限。
摘要滞后时优先保留最新原始消息；不得把完整历史或隐藏 Prompt 返回给客户端。

## 6. 异步提炼

worker 复用现有 MySQL、LLM、TEI、ES 和 Qdrant 生命周期，并增加一个周期
维护入口：

1. 短事务只领取每个会话最早的可用 outbox，按 LLM 并发上限分波写入
   lease token 与过期时间。
2. 加载同一 `user_id + conversation_id` 的旧摘要和有界消息。
3. 搜索同用户相近长期记忆，避免重复 key/value。
4. 使用现有 `ChatOpenAI.with_structured_output()` 一次返回：
   - 更新后的有界摘要；
   - 零到多条用户记忆候选。
5. 代码验证候选的消息归属、角色、精确用户原文证据和确认关系。
6. 新事务按 lease token compare-and-set：
   - 更新摘要及 `summary_through_message_id`，禁止游标倒退；
   - 调用现有 `MemoryRepository.upsert_candidates()`；
   - 写 ADD/UPDATE/SUPERSEDES 和 ES/Qdrant desired-state outbox；
   - 删除提炼 outbox。
7. 失败只记录安全异常类型，清除 lease 并按现有上限指数退避。

### 6.1 对话记忆契约

新增一个 `MemoryKind.USER_MEMORY`，内容为：

```text
category              PROFILE | PREFERENCE | CONSTRAINT | BUSINESS_RULE
key                   稳定、规范化的事实键
value                 自包含的事实值
supporting_user_quote 用户消息中的精确非空子串
evidence_message_uids 一组同用户消息 UID
confirmed_assistant_message_uid  可空
trust                 user_confirmed
```

直接用户事实必须引用用户消息精确原文。助手来源结论必须同时引用助手消息和
后续用户的明确确认原文；“可以”“好的”等无法唯一绑定具体结论的模糊确认
默认不产生记忆。

同一 `user_id + category + key` 形成稳定作用域。相同内容通过 hash 幂等，
不同值生成新 UID 并 `SUPERSEDES` 旧活动记忆。

## 7. 检索与索引隔离

`MemoryProjection`、ES mapping 和 Qdrant payload 增加 `user_id` 与来源字段。

- DDL 搜索：限制 DDL kinds，并要求 `user_id IS NULL`。
- 对话搜索：限制 `USER_MEMORY`，同时过滤固定 source 与非空精确 `user_id`。
- ES/Qdrant 返回 UID 后，MySQL `get_many_active()` 再用同一 `user_id`
  条件回查。
- pending index outbox、状态、内容/投影版本和 hash 校验保持不变。

投影版本从 `v1` 升级，部署时先更新 MySQL，再显式 recreate 项目专用
ES/Qdrant 索引并从 MySQL ACTIVE 记忆重建。

## 8. 删除与清理

### 删除会话

在 `user_id + conversation_uid` 条件下硬删除会话。MySQL FK 级联消息和未完成
提炼 outbox；已有用户长期记忆不变。

### 删除用户

一个 MySQL 事务：

1. 删除该用户所有会话、消息和提炼 outbox；
2. 将活动用户记忆标记为 DELETED，并把此前已软删除的用户记忆一并标记为
   待物理清理；
3. 设置 `purge_requested_at`；
4. 写入 ES/Qdrant DELETE desired state。

此后所有查询立即排除这些数据。维护任务只在两个索引 outbox 均确认后删除
相关 links、events 和 `agent_memory`，保证派生索引删除可重试。

## 9. 错误、并发与安全

- Pydantic 在 HTTP 边界拒绝空文本、超长文本、未知字段和非法角色。
- 所有会话、消息、outbox 和用户记忆 SQL 都包含 `user_id`。
- 在途轮次冲突、幂等内容冲突、删除对象和租户不匹配使用稳定安全错误码。
- 日志只记录 trace/turn/conversation 的不透明 ID、阶段、计数和异常类型，
  不记录 user_id、消息、摘要、记忆值或 Prompt。
- 对话提炼失败不改变消息可用性；索引失败不改变 MySQL 权威事实。
- 进程崩溃后的过期 lease 可被其他 worker 重新领取，稳定 UID 保证重放幂等。

## 10. 配置

新增 `conversation` 设置，仅保留确有运行边界的参数：

```yaml
conversation:
  max_message_chars: 32768
  context_message_limit: 20
  context_max_chars: 32768
  summary_max_chars: 4096
  extraction_batch_size: 10
  extraction_lease_seconds: 180
```

失败退避复用现有 `memory.outbox_max_backoff_seconds`，长期记忆检索数量复用
`memory.search_limit`，不增加保留期、Agent、附件或对象存储配置。

## 11. 迁移与回滚

- 更新 `docs/docker/mysql/data_agent.sql` 供空环境创建完整结构。
- 增加一次性、显式的 MySQL 升级 SQL，新增会话表及可空记忆字段，不修改
  Meta 四表。
- 升级 SQL 执行后校验列、索引、外键和 DDL 记忆行仍完整。
- bump `memory.projection_version`，recreate 并全量重建项目专用索引。
- 代码回滚时保留新增 MySQL 数据；旧代码忽略新表和可空列。旧投影版本不能
  混用，回滚后需按旧版本再次重建索引。

## 12. 验证重点

- 会话/消息 CRUD、keyset 分页、租户隔离、并发轮次门禁和幂等。
- 消息与提炼 outbox 原子性、lease 恢复、重试与重复执行。
- 用户原文证据、助手猜测拒绝、明确确认接受和摘要游标单调性。
- USER_MEMORY 的 hash、scope、supersede、历史、软删除、用户级 purge。
- ES/Qdrant `user_id` 过滤、MySQL 回查、降级与投影重建。
- 删除会话/用户和索引删除确认顺序。
- 现有 DDL workflow/checkpoint/Meta/memory 全量回归。
