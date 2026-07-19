# Store 职责拆分实施计划

## 1. Preparation

- [x] 读取 backend directory、Redis integration、error handling 和 quality specs。
- [x] 记录 `DDLJobStore` 的所有生产调用方、测试调用方及现有方法签名。
- [x] 运行拆分前 jobs 相关测试基线；live Redis/MySQL 不可用时记录环境限制。

## 2. Deterministic Foundations

- [x] 新增 `job_keys_store.py` 和 `JobKeysStore`，集中现有 Redis key/member/arq ID 格式。
- [x] 新增 `job_codec_store.py` 和 `JobCodecStore`，移动公开投影、问题集合 ID、问题/回答规范序列化逻辑。
- [x] 新增 `job_scripts_store.py` 和 `JobScriptsStore`，原样移动 Lua 协议并保持参数顺序和返回码。
- [x] 如确有多模块复用需要，仅新增 `redis_base_store.py` 承载窄职责 Redis awaitable 类型支持。
- [x] 新增无需 live Redis 的 jobs 单元测试，锁定 key 和 codec 兼容性。

## 3. Specialized Stores

- [x] 新增 `redis_job_state_store.py`，承接 job record 读取和原子 submit/transition/answer 操作。
- [x] 新增 `source_lease_store.py`，承接 renew 和 mutation lease。
- [x] 新增 `job_outbox_store.py`，承接 dispatch 和 checkpoint cleanup。
- [x] 保证专职组件依赖方向单向，不相互反向调用。
- [x] 检查 jobs store 模块均使用完整 `snake_case` 并以 `_store.py` 结尾，专职类均使用 `PascalCase` 并以 `Store` 结尾。

## 4. Compatibility Facade

- [x] 将 `store.py` 重命名并收敛为 `ddl_job_store.py`，承载 `DDLJobStore` 门面和业务生命周期编排。
- [x] 更新生产代码和测试导入路径，保持所有现有公开签名、错误映射、`question_set_id` 及测试所需内部 key 访问。
- [x] 确认 API、worker、memory service 和 application composition 无需改为多组件注入。
- [x] 搜索旧常量/重复 key/重复 codec 实现，确保单一来源。

## 5. Validation

- [x] `uv run pytest -m "not integration"`。
- [x] `uv run pytest tests/integration/test_api.py`。
- [x] `uv run pytest tests/integration/test_worker.py`。
- [x] `uv run pytest tests/integration/test_ddl_metadata_flow.py`。
- [x] `uv run ruff check src tests`。
- [x] `uv run pyright src tests`。
- [x] `uv run python -m compileall -q src tests`。
- [x] `uv run python -m data_agent.settings`。
- [x] `git diff --check`。

## 6. Review and Documentation

- [x] 按 `code_review.md` 检查原子协议、异常映射、敏感字段清理和测试有效性。
- [x] 更新 backend directory structure 与 Redis job state spec 的模块边界，保持英文规范文档要求。
- [x] 检查最终 diff 不包含 Redis 协议、公开 API 或业务行为的非预期变化。

## Risk and Rollback Points

- Lua 参数位置最敏感：必须原样搬移，任何重排都需逐项核对调用。
- facade 委托可能引入初始化或循环依赖：先建立底层模块，再收敛门面。
- live integration 依赖本机 Redis/MySQL；不可用不等于通过，需单独报告。
- 无数据迁移；发现行为偏差时可整体回退此次职责拆分。
