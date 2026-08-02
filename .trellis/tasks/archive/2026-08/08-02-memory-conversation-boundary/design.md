# Memory 与 Conversation 边界设计

## Purpose

本任务把 Conversation 和 Long-term Memory 的应用编排从 MySQL、Elasticsearch、Qdrant、TEI 与具体 repository 中解耦，同时保留现有跨上下文原子事务。重构围绕可观察用例建立少量深接口，不把现有 repository 的每个方法机械复制成端口。

## Current Evidence

- `ConversationService` 在应用方法内创建 MySQL session，并直接构造 `MemorySearchService`、`MemoryRepository`。
- `ConversationMemoryExtractor` 在一个 session 中直接执行 Memory candidate upsert 与 Conversation extraction finish；该原子性必须保留。
- `MemoryService` 和 `MemorySearchService` 直接创建 MySQL session、repositories、ES/Qdrant/TEI adapters，并读取全局配置。
- `MemoryIndexDispatcher` 将 claim/authority、外部索引写入、结算与具体客户端构造集中在一个模块。
- 当前搜索与 dispatcher 测试大量 patch 模块全局 concrete collaborators；新的 public seam 覆盖行为后应替换这些测试。

## Target Modules

```text
src/data_agent/
├── conversation/
│   ├── application/
│   │   ├── contracts.py
│   │   ├── service.py
│   │   └── extraction.py
│   └── adapters/mysql/
│       ├── store.py
│       ├── user_data.py
│       └── extraction_commit.py
└── memory/
    ├── application/
    │   ├── contracts.py
    │   ├── service.py
    │   ├── search.py
    │   └── index_dispatcher.py
    └── adapters/
        ├── mysql.py
        ├── search_indexes.py
        ├── projection_indexes.py
        └── composition.py
```

只创建有真实生产适配器与测试替身的接口；现有 MySQL tables/repository、ES/Qdrant mappings 仍留在对应 adapter 层，不迁移 schema。

## Conversation Ports

`ConversationStore` 以用例级方法封装 create/list/history/delete/start-turn/complete-turn 的短事务，应用层不接收 `AsyncSession`。`ConversationService` 依赖注入该 store 和 `LongTermMemoryReader`：start-turn 的用户消息与 active-turn gate 先提交，随后事务外 recall；complete-turn 的 assistant message、extraction outbox 与 gate release 仍原子提交。

Conversation 删除单个会话继续只删除 Conversation 权威状态，不触碰跨会话共享长期记忆。

`UserDataEraser.erase(user_id)` 是一个外层 MySQL integration adapter：在同一 transaction 中调用 Conversation repository 删除用户会话/outbox，再调用 Memory repository tombstone 用户记忆。Conversation 应用层只依赖该深接口，不导入 `memory.mysql`。

`ExtractionCommitter.commit(claim, candidates)` 是另一个外层 MySQL integration adapter：在同一 transaction 中 upsert Memory candidates 并完成 Conversation extraction summary/outbox。模型调用、quote/role/message UID/时序验证和失败退避仍在事务外；应用层不暴露 session。

## Long-term Memory Ports

`MemoryStore` 提供服务用例所需的领域操作和事务语义：读取、历史、受租约保护的替换、软删除。生产 adapter 为每个调用创建短 MySQL transaction；应用层只处理 Memory domain values、错误映射和结果投影。

`MemorySearchStore` 提供 exact baseline、authoritative active read、pending projection targets 与 best-effort access recording。`LexicalMemoryIndex`、`VectorMemoryIndex` 和 `EmbeddingProvider` 只返回候选 UID/分数；`MemorySearchService` 通过构造参数接收 top-k、timeout、RRF 和版本配置。搜索仍遵循：MySQL exact baseline；远程信号独立降级；最终 MySQL 权威回读；pending outbox 信号剔除；tenant/source/status/hash/version/expiry/object whitelist 二次校验。

`MemoryProjectionWorkStore` 与 `MemoryProjectionIndex` 支持 dispatcher 的 claim -> remote -> settle。claim/renew/ack/retry/convergence 都是短 MySQL transaction，ES/Qdrant/TEI 调用在事务外；权威状态变化时不得确认迟到写入，并必须恢复 durable convergence。

## Composition

HTTP application lifespan 和 DDL worker startup 是唯一 composition roots。它们在基础设施客户端初始化后构造 Memory adapters、services、Conversation adapters/services 和 dispatcher，并把长生命周期实例注入 API/Chat/worker ctx。maintenance cron 不再自行构造 Memory dispatcher 或直接创建 MemoryRepository。

Memory 的 expire/purge maintenance 通过注入的 Memory maintenance use case 执行；它仍属于 Long-term Memory，不移动到 Conversation。

## Dependency Rules

- `conversation.application` 不导入 `memory.mysql`、`infrastructure.mysql`、SQLAlchemy 或全局 settings。
- `memory.application` 不导入 `memory.mysql`、`infrastructure.*`、外部 SDK、SQLAlchemy 或全局 settings。
- Conversation 与 Memory 通过应用接口、稳定 IDs、Memory domain command 或外层 integration adapter 协作。
- MySQL integration adapters 可以组合两个 context 的 repositories，以保存已存在且明确要求的单事务原子性；该例外不得扩散到内层。

## Test Replacement

- Conversation public seam：turn lifecycle、context recall、complete-turn、conversation-only delete、atomic user-data erase。
- Extraction seam：claim/model/validate/atomic commit/retry；保留 quote、role、UID 与时间顺序证据测试。
- Memory service/search seam：authoritative read、exact baseline、remote degradation、pending signal filtering、tenant/expiry/hash/version guards、access-stat best effort。
- Memory projection seam：claim-remote-settle、delete/non-active convergence、authority loss、per-target retry、dead-letter。
- 保留 repository、SQL、ES mapping、Qdrant schema、lease/idempotency 等 adapter contracts。
- 新 seam 变绿后，在同一切片删除 patch 全局 concrete constructors、私有 helper 或 collaborator call-order 测试；事务顺序只有本身是契约时才保留。

## Compatibility and Migration

- HTTP payload/routes、Conversation/Memory Pydantic contracts、MySQL schemas、Redis keys、index names/mappings、配置键和日志事件保持不变。
- 不增加数据库、索引或历史数据迁移、双写、兼容 shim 或清理路径。
- 内部模块移动为硬迁移，活动代码和测试同步更新。

## Verification

运行 Ruff、Pyright、compileall、非集成 pytest、Conversation/Memory focused suites、相关 MySQL/Redis/ES/Qdrant integration tests、禁止依赖搜索、AST import-cycle 检查和 package build。外部服务或本地 schema 不匹配必须如实记录，不通过修改数据环境规避。
