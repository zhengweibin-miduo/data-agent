# Store 职责拆分设计

## 1. Design Goal

在不改变 `DDLJobStore` 对外行为及 Redis 协议的前提下，将任务存储实现拆成可独立理解和测试的专职模块。拆分以职责和依赖方向为依据，不改变部署边界，也不引入新的抽象层级到 API、worker 或 memory service。

## 2. Current Problem

`src/data_agent/ddl_metadata/jobs/store.py` 当前约 590 行，包含以下不同变化原因：

- Redis key 格式与成员格式；
- Lua 原子提交、转换、回答、续租和释放协议；
- Redis 客户端返回类型收窄；
- `JobRecord` 投影与问题/回答规范序列化；
- 任务状态转换和业务错误；
- 来源租约；
- arq dispatch outbox；
- waiting deadline sweep；
- checkpoint cleanup outbox。

任何一类协议变化都需要在同一个大文件中定位，且确定性逻辑缺少无需 live Redis 的聚焦测试。

## 3. Target Modules

```text
ddl_metadata/jobs/
├── __init__.py
├── job_codec_store.py       # Job Hash 投影、问题/回答规范序列化与摘要
├── job_keys_store.py        # Redis job/source/dispatch/waiting/cleanup keyspace
├── job_scripts_store.py     # 多键原子 Lua 协议常量
├── redis_job_state_store.py # Redis job record 读写和原子状态/回答操作
├── source_lease_store.py    # 来源租约续期及 memory mutation 临时租约
├── job_outbox_store.py      # arq dispatch 与 checkpoint cleanup outbox
└── ddl_job_store.py         # DDLJobStore 兼容门面和任务生命周期编排
```

所有职责模块使用完整 `snake_case` 并统一以 `_store.py` 结尾，对应专职类使用 `PascalCase` 并统一以 `Store` 结尾。如果实施中发现 `redis-py` 同步/异步联合返回类型需要被多个组件共享，应由 `redis_base_store.py` 承载窄职责支持；不得增加不符合命名约束的模块，也不得放入无边界的通用 `utils.py`。

## 4. Object Boundaries

### 4.1 `JobKeysStore`

不可变地持有 prefix，生成：

- `job(job_id)`；
- `source(source)`；
- `dispatch`；
- `waiting`；
- `checkpoint_cleanup`；
- `activation_member(job_id, revision)`；
- 确定性 arq job ID。

所有字符串格式保持现状。

### 4.2 `JobCodecStore`

无实例状态的专职 Store 通过静态方法承载确定性转换：

- `Mapping[str, str] -> JobRecord`；
- 问题集合规范 JSON 与 `sha256:` 标识；
- 回答规范 JSON 与摘要。

`question_set_id` 与 `DDLJobStore` 统一从 `jobs.ddl_job_store` 导入；实现来源位于 `job_codec_store.py`。

### 4.3 `JobScriptsStore`

集中保存提交、转换、回答、续租和安全释放 Lua 脚本。脚本只通过明确命名的类属性提供，不创建运行时状态，不改变脚本文本、参数位置或返回码。

### 4.4 `RedisJobStateStore`

只拥有 job Hash 和跨 job/source/waiting/cleanup/dispatch 的原子 Redis 协议：

- 原子提交；
- 读取公开投影、执行输入和内部回答；
- 修订感知转换；
- 原子回答提交。

Lua 脚本仍以单次 `EVAL` 保持多键一致性。该组件返回 typed record/result，不向上层泄漏原始 Hash。

### 4.5 `SourceLeaseStore`

只拥有来源租约：

- 当前 job 所有者续租；
- memory mutation 的短时互斥租约；
- token 匹配后的安全释放。

它复用相同 source key，因此仍与活动任务互斥。

### 4.6 `JobOutboxStore`

只拥有：

- dispatch outbox 读取、arq 幂等入队和成功后移除；
- checkpoint cleanup outbox 读取和确认。

等待超时 sweep 需要调用状态转换，因此由 `DDLJobStore` 门面编排，不让 outbox 组件反向依赖状态组件。

### 4.7 `DDLJobStore`

保持现有构造方式和公开方法签名，组合上述组件。门面拥有业务级编排：

- DDL 大小限制和提交错误；
- allowed transition 规则；
- `mark_running` / `mark_waiting` / `mark_terminal`；
- 回答 ID 业务校验及错误映射；
- waiting deadline sweep；
- 对 lease/outbox 方法的兼容委托。

为当前集成测试保留 `_redis`、`dispatch_key`、`waiting_key`、`cleanup_key`、`_job_key()` 和 `_source_key()` 的兼容访问，但生产代码不得新增对这些内部入口的依赖。

## 5. Dependency Direction

```text
API / worker / memory service
            |
       DDLJobStore
       /    |     \
 state   lease   outbox
   |       |       |
 job_keys_store / job_scripts_store / job_codec_store / redis_base_store
```

底层模块不依赖 FastAPI、worker 或 application composition。`outbox_store` 可以依赖 arq 的 `ArqRedis` 协议，但不依赖 worker 函数。

## 6. Compatibility and Invariants

- 不修改 Redis prefix、key、member、Hash field、TTL 和 Lua 返回码。
- 不修改 `DDLJobStore` 构造函数及现有公开方法；调用方只迁移到明确的 `jobs.ddl_job_store` 导入路径。
- 不修改 `question_set_id` 的规范 JSON 或 hash 格式。
- 不修改 `DDLMetadataError` code/stage/http status。
- submission、answer submission、terminal transition 继续分别以一次 Lua 调用完成其多键原子写入。
- arq `_job_id` 继续为 `{prefix}:{job_id}:{revision}`。
- package `__init__.py` 不重导入应用对象，不产生配置或连接副作用。

## 7. Testing Strategy

- 新增 jobs 单元测试，覆盖 keyspace、投影、问题/回答规范化及非法状态转换等确定性行为。
- 现有 `tests/integration/test_api.py` 覆盖 HTTP 映射及 Redis 不可用行为。
- 现有 `tests/integration/test_worker.py` 覆盖转换、回答竞争、等待过期、dispatch 和 cleanup。
- 现有 `tests/integration/test_ddl_metadata_flow.py` 覆盖 Redis + MySQL + LangGraph 完整路径。
- 全量静态门禁覆盖模块依赖、类型、Docstring 和导入循环。

## 8. Rollback

此次改动不迁移数据。若拆分造成回归，可回退模块拆分提交并恢复单文件实现；既有 Redis 数据无需转换。实施期间保持每一步都是纯代码组织变化，避免混入协议变更。
