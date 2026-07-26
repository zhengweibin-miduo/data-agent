# DDL 任务/工作流注释定位清单

以下位置是读者仅凭语句难以推导的流程约束，建议新增简短中文 rationale/invariant 注释；不改运行时逻辑。

1. **API 受理与 outbox 原子性** — `src/data_agent/ddl_metadata/jobs/store.py:77-105`，`DDLJobStore.submit`。说明只有 `_state.submit` 同时写任务和 dispatch outbox 成功后才返回 202；发布公开 queued 事件失败不应撤销已受理任务。证据：`api/jobs.py:29-45` 直接返回 accepted；集成流程 `tests/integration/test_worker.py:81` 覆盖 `source_busy`。

2. **来源单活租约与终态释放** — `jobs/store.py:304-312`（`renew_source_lease`、`mutation_lease`）及 `jobs/store.py:281-302`（`mark_terminal`）。解释续租必须由当前 job 所有者校验，终态转换负责释放 source lease/保留结果，避免并发图任务或浏览器记忆写入互相覆盖。证据：`tests/integration/test_worker.py:284` 调用 runner；`tests/integration/test_ddl_metadata_flow.py` 覆盖完整生命周期。

3. **回答 revision/question-set 幂等边界** — `jobs/store.py:222-279`，`submit_answers`。注明先校验当前问题 ID，再由 Redis 原子脚本比较 revision 与 question_set_id；返回 1 才首次入队，重复提交仅返回现状，过期返回 410、陈旧返回 409。证据：`tests/integration/test_worker.py:140-196` 验证 invalid/stale/repeated answer。

4. **状态转换的集中不变量** — `jobs/store.py:159-184`，`transition`。注释说明 revision 条件与 `_ALLOWED_TRANSITIONS` 是并发 worker 的 CAS 门闩；状态事件仅在原子更新成功后发布，防止旧修订覆盖新状态。调用证据：`mark_running`、`mark_waiting`、`mark_terminal`（186-302）。

5. **checkpoint interrupt/resume 分支选择** — `worker/job_runner.py:346-385`，`run_ddl_job`。解释 `aget_state` 后按“无快照→初始输入；有 interrupt→Command(resume)；无 next→投影并清理；否则继续图”分支，必须使用同一 `thread_id=job_id` 才能恢复并避免重复模型调用。测试：`tests/unit/ddl_metadata/workflow/test_graph.py:21`、`tests/integration/test_ddl_metadata_flow.py:69-149`。

6. **同步 checkpoint durability 与公开进度** — `worker/job_runner.py:388-406`。建议说明 `durability="sync"` 保证节点结果在 worker 返回前落盘；`stream_mode="tasks"` 只用于发布稳定 stage，不泄露节点输入/输出。证据：`jobs/store.py:138-149` 的 progress contract；`tests/integration/test_job_events.py` 覆盖事件流。

7. **可重试异常与指数退避上限** — `worker/job_runner.py:407-438`。注释应说明仅 `_RETRYABLE` 且 `attempt < 3` 时把 RUNNING 回滚 PENDING 并重试，指数退避加 jitter；超过上限才写 FAILED，避免重复已完成节点。质量规范也要求 persistence retry 不重复 model call（`quality-guidelines.md:127-128`）。

8. **图版本不匹配的不可重试终态** — `worker/job_runner.py:260-286`。解释新 graph_version 不允许恢复旧 checkpoint；PENDING 先标 RUNNING 以取得 attempt，再 FAILED 并清理 checkpoint，避免旧状态被新图解释。可与 `tests/unit/ddl_metadata/test_package_contracts.py:106-156` 的 runner/config 契约核对。

9. **终态 checkpoint 清理 outbox** — `worker/job_runner.py:285-286,332-333,383-406,452-464` 与 `worker/maintenance.py:31-36`（`cleanup_checkpoints`）。注释应说明清理采用独立 outbox/cron，不能在业务终态事务中直接删除；worker 崩溃后由维护任务重放，且只删除已终态且确认的 thread。证据：`tests/integration/test_worker.py:480-501`。

10. **工作流节点顺序及分支原因** — `workflow/graph.py:21-49`，`build_ddl_metadata_graph`。建议在边定义前说明 parse→memory reuse→classify/validate→questions interrupt→generate/validate→memory candidates→persist 的顺序：确定性校验必须包住模型输出，persist 只能接收 finalized 数据；conditional routing 依赖各节点写入的 `route`。集成测试标题 `tests/integration/test_ddl_metadata_flow.py:69`。

11. **语义/指标修复只允许一次** — `workflow/nodes.py:136-210,313-438`，`classify_node`/`validate_metadata_node`/`generate_metrics_node`/`validate_metrics_node`。注释说明 `semantic_attempts`、`metric_attempts` 是 checkpoint 内的重试预算；仅 repairable issues 可回环一次，第二次仍失败转结构化 DDLMetadataError，防止无限 LLM 循环。测试契约见 `tests/unit/ddl_metadata/workflow/test_graph.py`。

12. **interrupt 问题集合与答案合并** — `workflow/nodes.py:284-311`，`await_answers_node`。解释 interrupt payload 中的 question_set_id/round 是外部提交的版本锚点；resume 后按 question_id 合并历史答案，避免重复回答丢失，并由 store 再做原子 revision 校验。证据：`tests/integration/test_ddl_metadata_flow.py:117-149`。

13. **持久化是唯一成功出口** — `workflow/nodes.py:440-520`，`build_memories_node`/`persist_node`。建议说明只有 validate_metrics 完成后才构建 accepted memory candidates，`snapshot.persist` 成功才写 `SUCCEEDED` result；任何异常由 runner 统一转 FAILED。证据：`worker/job_runner.py:398-406,452-464`。

## 不应添加的注释

- `workflow/graph.py:28-37` 每个 `add_node` 的逐行中文翻译；节点名已自解释且会与 Docstring 重复。
- `jobs/store.py:80-83,90-104` 等简单长度判断、UUID 生成、字段传递；语句直接表达行为。
- `workflow/nodes.py` 中各类 `model_validate`、列表推导、`model_dump` 的机械解释。
- `worker/job_runner.py:395-397` 对 `publish_progress` 调用本身的复述；应只解释公开 stage 与内部 payload 隔离（若需）。
- 测试 fixture/断言准备步骤，以及简单 getter、Redis key 拼接、codec 序列化；不增加业务不变量。
