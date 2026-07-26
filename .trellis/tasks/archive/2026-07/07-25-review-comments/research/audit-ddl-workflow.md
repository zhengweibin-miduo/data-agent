# DDL 任务与工作流分区审查

## 审查文件清单

生产源码（排除 `ddl_metadata/memory/**` 与 `ddl_metadata/models/memory.py`）：

`src/data_agent/ddl_metadata/__init__.py`、`errors.py`、`identifiers.py`、`parsing.py`、`validation.py`；`api/{__init__,job_events,jobs,memories,router}.py`；`jobs/{__init__,identifiers,store}.py`；`jobs/redis/{__init__,base,codec,event_store,keys,lease_store,outbox_store,scripts,state_store}.py`；`models/{__init__,base,jobs,physical,semantic}.py`；`persistence/{__init__,metadata_repository,schema,tables}.py`；`worker/{__init__,job_runner,lifecycle,maintenance,settings}.py`；`workflow/{__init__,contracts,graph,llm_metadata_generator,nodes,routing,state}.py`。

对应测试：`tests/integration/test_ddl_metadata_flow.py`；`tests/unit/ddl_metadata/{__init__,test_job_events_api,test_job_events,test_package_contracts,test_parsing,test_validation}.py`；`tests/unit/ddl_metadata/jobs/{__init__}.py`；`tests/unit/ddl_metadata/jobs/redis/{__init__,test_event_store,test_job_stores}.py`；`tests/unit/ddl_metadata/worker/{__init__,test_job_runner}.py`；`tests/unit/ddl_metadata/workflow/{__init__,test_graph}.py`。

## P0/P1 候选

未发现。逐点核对任务状态转换、revision guard、source lease、dispatch/checkpoint cleanup outbox、worker retry、LangGraph interrupt/resume、`parse_ddl` `to_thread` 边界及事务 Docstring；未找到能由当前实现和调用方证明的 P0/P1 语义矛盾。

## 非阻塞候选

未形成可确认维护问题。`parse_ddl` 的 Docstring（`src/data_agent/ddl_metadata/parsing.py:275`）表述“在线程边界外解析”，实现确实通过 `asyncio.to_thread`（`:276`）执行；属于措辞可读性而非错误。其余短 Docstring 与签名/实现一致，未将机械复述单独报为问题。

## 确认无问题项

- `DDLJobStore.submit`（`jobs/store.py:77-105`）先经 Redis 原子 submit 写入任务与 dispatch outbox，再读取记录并安全发布 queued 事件；发布失败不改变已受理状态。
- `run_ddl_job`（`worker/job_runner.py:232-459`）在终态、revision mismatch、lease 丢失、重试和 interrupt/resume 分支均有显式状态保护；恢复回答使用 `Command(resume=...)`（`:361-374`）。
- `await_answers_node`（`workflow/nodes.py:284-311`）按 question id 合并跨轮回答；`plan_questions_node`（`:212-282`）维护历史问题和轮次。
- `parse_ddl`（`parsing.py:270-276`）完整解析、规范化、哈希和模型构造均位于 `_parse_ddl_sync`，异步边界仅负责 `to_thread` 调度，符合后端规范。
- 事务边界 Docstring 与实现一致：`workflow/contracts.py:80`、`persistence/metadata_repository.py:23-26` 明确由调用方管理事务。

## 验证

执行：`uv run pytest -q tests/unit/ddl_metadata --disable-warnings --maxfail=1`；结果 `34 passed`。未执行 live MySQL/Redis 集成流。产品源码与测试未修改。
