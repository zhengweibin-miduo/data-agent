# 基于 Mem0 的 Data Agent 记忆架构设计

## 1. 设计目标

以 `mem0ai/mem0` 的开源记忆抽取、身份作用域、ADD-only、历史、实体链接和多信号检索为参考，将当前错误适配自 `usememos/memos` 的长期记忆实现重建为面向 DDL 语义元数据的 AI 记忆系统。

本设计不直接集成 `mem0ai` SDK。Data Agent 继续拥有模型、事务、校验、基础设施和 API 边界，并保留以下不可妥协约束：

- DDL AST 和确定性规则是物理事实的最终权威；
- 只有完整工作流接受的结果能够成为长期记忆；
- Meta、MySQL 权威记忆和索引 outbox 原子提交；
- Elasticsearch 与 Qdrant 都是可重建投影；
- LangGraph checkpoint 只负责活动任务工作状态与恢复。

参考基线固定为 `mem0ai/mem0@ddaa655edf41e3ed375b263fb227da0bcd42ccb9d`。

## 2. 参考与领域化边界

### 2.1 直接借鉴

- 以身份过滤限定记忆作用域；
- 将输入转为自包含、可检索的事实；
- ADD-only 自动抽取思想；
- 内容哈希去重；
- 记忆历史；
- 记忆之间的链接；
- embedding、向量检索、关键词检索和实体信号；
- search/get/history/update/delete 能力。

### 2.2 领域化改造

- Mem0 的 `user_id` 映射为逻辑 DDL `source`；
- `run_id` 映射为生成来源 `job_id`，只作 provenance，不作长期召回边界；
- “消息抽取”由现有结构化生成与确定性校验取代，不再增加一次通用 LLM 抽取；
- 表、列和指标稳定 ID 取代通用自然语言实体抽取；
- 更新不是直接改写 Meta，而是记录用户确认修正并要求 DDL 重处理；
- 删除为可审计软删除；
- 召回结果必须回查 MySQL 并通过当前 AST、Pydantic 和业务规则重校验。

### 2.3 不采用

- `usememos/memos` 的笔记、pin/archive、附件、评论和社交产品模型；
- Mem0 托管平台闭源排序、时间推理和自动衰减；
- 任意用户文本直接 add 为可信长期事实；
- 向量库作为权威数据库；
- 隐藏思维链、原始 Prompt、完整模型响应或无界任务记录。

## 3. 三层记忆

### 3.1 工作记忆

Redis LangGraph checkpoint 按 `thread_id=job_id` 保存节点、路由、重试、校验错误和 interrupt/resume 状态。生命周期只覆盖一次任务，终态后通过 cleanup outbox 删除。

### 3.2 会话/情景记忆

当前 DDL、问题、回答、已验证中间结果和待提交记忆候选仍是 graph state。它们可以在任务恢复时继续使用，但在成功持久化前不得进入长期召回索引。

### 3.3 长期语义记忆

MySQL 保存跨任务权威事实和历史。Elasticsearch 保存 BM25 投影，Qdrant 保存 TEI embedding 投影。长期作用域是 `source`，对象粒度由稳定表、列、问题和指标 ID 表达。

## 4. 权威数据模型

旧 `llm_memory` / `llm_memory_relation` 契约直接废弃，不提供旧数据兼容。新的应用数据库表建议如下。

### 4.1 `agent_memory`

```text
id                  BIGINT AUTO_INCREMENT PRIMARY KEY
uid                 CHAR(64) UNIQUE
source              VARCHAR(128)
kind                VARCHAR(32)
scope_key           VARCHAR(256)
schema_fingerprint  CHAR(64)
memory_text         TEXT
content             JSON
content_hash        CHAR(64)
trust               VARCHAR(32)
status              VARCHAR(16)        # ACTIVE / DELETED
content_version     VARCHAR(32)
projection_version  VARCHAR(32)
created_job_id      CHAR(64)
created_at          DATETIME
updated_at          DATETIME
deleted_at          DATETIME NULL
```

`content` 是当前 Pydantic 类型化事实；`memory_text` 是从 content 确定性生成的有界检索文本；`content_hash` 用于幂等去重。检索文本和向量不得反向覆盖 content。

### 4.2 `agent_memory_event`

```text
id              BIGINT AUTO_INCREMENT PRIMARY KEY
memory_id       BIGINT
event_type      VARCHAR(16)        # ADD / UPDATE / DELETE / LINK
old_content     JSON NULL
new_content     JSON NULL
job_id          CHAR(64) NULL
actor_type      VARCHAR(16)        # WORKFLOW / USER / SYSTEM
created_at      DATETIME
```

历史只追加。工作流初次接受记录 ADD；用户修正记录 UPDATE 事件，但修改后的事实只有在下一次完整 DDL 重处理后才能成为 Meta 及活动检索内容；软删除记录 DELETE。

### 4.3 `agent_memory_link`

```text
memory_id          BIGINT
linked_memory_id   BIGINT
link_type          VARCHAR(32)     # RELATED / DERIVED_FROM / SUPERSEDES
PRIMARY KEY (memory_id, linked_memory_id, link_type)
```

链接表达问题、回答、表列决策和指标之间的来源关系。它是 Mem0 linked memory 的领域化实现，不保存任意社交评论。

### 4.4 `memory_index_outbox`

```text
memory_uid         CHAR(64)
target             VARCHAR(16)     # ELASTICSEARCH / QDRANT
operation          VARCHAR(16)     # UPSERT / DELETE
projection_version VARCHAR(32)
attempts           INT
available_at       DATETIME
last_error_type    VARCHAR(128) NULL
updated_at         DATETIME
PRIMARY KEY (memory_uid, target)
```

同一事务写入目标期望状态。每个目标独立确认，避免 ES 成功而 Qdrant 失败时丢失重试。新事件覆盖同一 UID/target 的旧期望操作。

## 5. 记忆写入

```text
LangGraph 最终校验
  -> 类型化 accepted facts
  -> 确定性生成 memory_text / hash / links
  -> 同一 MySQL Session:
       Meta snapshot
       agent_memory
       agent_memory_event
       agent_memory_link
       memory_index_outbox(ES + Qdrant)
  -> commit
  -> DDL 任务成功
```

稳定 UID 与内容哈希保证 checkpoint 重放后的幂等。失败、拒绝、过期或未完成任务不写权威记忆和 outbox。

用户 update 创建用户确认事件及待应用内容，返回 `requires_reprocess=true`；它不直接修改 Meta。下一次 DDL 任务验证并接受后，新的活动事实通过 ADD/UPDATE 与 `SUPERSEDES` 链接生效。

用户 delete 在来源租约内把权威记录标为 DELETED、追加 DELETE 事件并写入两个 DELETE outbox；现有 Meta 不被静默修改。

## 6. 派生索引同步

arq worker 增加周期性索引 dispatcher：

1. 从 MySQL 有界领取可执行 outbox 行；
2. Elasticsearch UPSERT 直接使用 `memory_text` 与过滤字段；
3. Qdrant UPSERT 先调用 TEI 生成 document embedding，再写 point；
4. DELETE 分别删除 ES document 和 Qdrant point；
5. 每个 target 成功后独立确认；
6. 失败记录安全异常类型并指数退避；
7. DDL 主任务状态不因派生索引失败回滚。

并发领取使用数据库级互斥/`FOR UPDATE SKIP LOCKED` 或等价有界 claim，禁止两个 worker 同时确认同一目标。dispatcher 不记录 memory_text、DDL、用户回答或完整服务 URL。

全量重建按 MySQL 主键游标扫描 ACTIVE 记忆，重新写入两个目标的 UPSERT outbox。重建开始时显式重建指定 ES index 与 Qdrant collection；不得删除共享服务中的其他索引或 collection。

## 7. 派生索引

### 7.1 Elasticsearch

索引配置至少包含：

- `memory_text`：中文 text，使用已部署 IK analyzer；
- `memory_uid`、`source`、`kind`、`scope_key`、`object_ids`、`trust`、`status`、版本字段：keyword；
- `created_at`、`updated_at`：date。

查询必须过滤 `source`、ACTIVE、内容/投影版本和允许的 kind，再对 memory_text 执行 BM25。

### 7.2 Qdrant

point ID 使用稳定 memory UID；vector 由 TEI document embedding 产生。payload 与 ES 保持同一组过滤字段，并为 source/kind/status/version 建 payload index。

查询 embedding 使用现有 BGE query instruction。collection 的向量维度和距离度量必须与 TEI 实际输出一致，由设置与启动检查验证。

## 8. 混合召回

### 8.1 查询构造

对当前 DDL 的表、列或指标作用域，确定性构造有界查询文本，包含名称、类型、DDL comment 和稳定语义上下文；不把完整 DDL 或历史记录发送到索引。

### 8.2 召回与融合

1. MySQL exact fingerprint 查询作为安全基线；
2. ES 和 Qdrant 在相同过滤条件下并发 top-k；
3. 使用 Reciprocal Rank Fusion 合并不同分数量纲：
   `score = Σ 1 / (60 + rank)`；
4. 完全对象 ID 匹配和 exact fingerprint 命中作为确定性加分；
5. 以融合分数、确定性匹配级别、UID 做稳定排序；
6. 批量回查 MySQL；
7. 排除 DELETED、版本不兼容、content hash 不匹配或 outbox 仍代表更新中的记录；
8. 重新执行 Pydantic、AST/reference 和业务校验；
9. 只向模型传递有界、无冲突的类型化 capsule。

如果 ES、Qdrant 或 TEI 不可用，记录降级并使用仍可验证的来源；两个索引都不可用时回退 MySQL exact。绝不直接信任索引 payload。

## 9. API

建议将旧 Memos 风格管理接口替换为：

```http
GET    /api/v1/metadata/memories/search
GET    /api/v1/metadata/memories/{memory_uid}
GET    /api/v1/metadata/memories/{memory_uid}/history
PATCH  /api/v1/metadata/memories/{memory_uid}
DELETE /api/v1/metadata/memories/{memory_uid}
```

- search 要求 source 和有界 query，支持 kind、limit、cursor/offset 的受限参数；
- get 从 MySQL 返回权威内容，不从 ES/Qdrant返回详情；
- history 有界分页；
- PATCH 只接受与原 kind/scope 相同的结构化用户修正；
- DELETE 是软删除；
- 不提供任意 POST add。

所有修改操作继续使用与 DDL job 共享的 source mutation lease。

## 10. 进程与生命周期

- FastAPI lifespan：初始化 MySQL、Redis、ES、Qdrant、TEI；外部检索故障由 search 服务降级，不影响只读权威详情。
- worker startup：初始化 MySQL、Redis、LLM、checkpoint、ES、Qdrant、TEI；索引 setup/dispatcher 的失败不得阻止 DDL 核心任务恢复，但必须形成可观察的积压。
- shutdown：按依赖逆序关闭新增客户端。
- worker 周期任务：dispatch index outbox、重建批次、原有 waiting/checkpoint cleanup。

## 11. 配置

新增或扩展配置：

- ES memory index name、analyzer、top-k；
- Qdrant memory collection、vector size、distance、top-k；
- memory projection version；
- RRF 常量和最终 limit；
- outbox batch size、最大退避；
- 检索超时；
- 重建 batch size。

配置必须集中在 `settings.py` / `app_config.yaml`，禁止散落常量描述同一契约。服务名称和凭据继续通过现有配置与环境边界管理。

## 12. 失败与恢复

| 场景 | 行为 |
|---|---|
| Meta/权威记忆/outbox 任一 MySQL 写失败 | 整体回滚，worker 按原异常分类 |
| MySQL commit 后 ES 失败 | DDL 成功，ES outbox 保留并退避 |
| MySQL commit 后 TEI/Qdrant 失败 | DDL 成功，Qdrant outbox 保留并退避 |
| 只有一个索引可用 | 使用可用结果并回查 MySQL |
| 两个索引都不可用 | 回退 MySQL exact |
| 索引返回旧/删除 UID | MySQL复核后丢弃 |
| 重复 dispatcher | 幂等 UID upsert/delete |
| worker 崩溃于外部写后、确认前 | 重放相同 desired operation |
| 用户修正与运行任务并发 | source lease 返回冲突 |
| 旧 checkpoint 恢复 | graph/content/projection 版本门禁阻止盲目复用 |

## 13. 迁移与回滚

用户确认现有长期记忆尚未使用，因此采用硬切换：

1. 修改 bootstrap SQL 与 SQLAlchemy Core 定义；
2. 仅删除旧长期记忆表/关系和本项目专用索引；
3. 创建新权威表、历史、链接和 outbox；
4. 创建空 ES index 与 Qdrant collection；
5. 不迁移旧 `llm_memory` 数据；
6. 保留 Meta 四表及其数据。

回滚代码时只能回滚到空的旧记忆模型；不得尝试把新事件/向量反向转换为旧 Memos 风格关系。

## 14. 测试策略

- 纯函数：memory_text、hash、scope、RRF、稳定排序和过滤；
- 仓储：ADD 幂等、历史、链接、软删除、outbox 原子性；
- 服务：search 降级、MySQL复核、冲突排除、用户修正；
- 基础设施：ES mapping/BM25、Qdrant collection/filter/vector、TEI 维度；
- worker：outbox 独立确认、退避、崩溃重放和重建；
- 全流程：DDL -> checkpoint -> accepted memory -> outbox -> hybrid recall；
- 回归：interrupt/resume、Meta rollback、重复 DDL、来源租约和 checkpoint cleanup。
