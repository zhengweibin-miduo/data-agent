# 为核心业务流程补充中文注释：实施计划

## 1. 实现

- [x] 阅读每个目标文件的完整待改函数，确认注释与实际控制流一致。
- [x] 在 `ddl_metadata/api/jobs.py` 与 `ddl_metadata/jobs/store.py` 补充受理、outbox 和 SSE 公开投影边界说明。
- [x] 在 `ddl_metadata/worker/job_runner.py` 补充执行守卫、checkpoint 恢复、interrupt/resume、投影和重试/终态说明。
- [x] 在 `ddl_metadata/workflow/graph.py` 补充拓扑阶段与唯一持久化出口总览。
- [x] 在 `conversation/service.py` 补充消息事务和三源有界上下文说明。
- [x] 在 `application.py` 补充资源初始化、服务装配和逆序关闭说明。
- [x] 在 `ddl_metadata/persistence/snapshots.py` 与 `metadata_repository.py` 补充作用域过期、Meta/记忆/outbox 原子顺序和范围内清理说明。
- [x] 检查产品 diff，移除重复、复述代码或与六类流程无关的注释。
- [x] 重新审计核心复杂函数，在连续筛选、校验、状态转换和持久化阶段补充分步骤中文注释。
- [x] 优先完善 `conversation/extraction.py::_validated_candidates` 的候选规范化、证据验证、助手结论确认、作用域去重与记忆构建步骤。
- [x] 在 `conversation/repository.py` 的 turn、提炼 claim/finish/retry 事务中补充阶段注释，并完善 `service.py::_context`。
- [x] 在 `ddl_metadata/workflow/nodes.py`、`api/job_events.py` 和 `parsing.py` 的核心流水中补充阶段注释。
- [x] 在 `memory/mysql/repository.py::upsert_candidates`、过期/删除生命周期和 `memory/application/search.py::search` 中补充阶段注释。
- [x] 对新增定位的同类复杂函数实施同一注释密度，并避免把简单条件逐行注释。
- [x] 全量审计 `src/data_agent/` 的业务 CRUD，为读取、校验、写入、回读或删除阶段增加编号步骤注释。
- [x] 全量审计生产代码说明性行内注释，删除或并入步骤注释；保留工具指令。
- [x] 验证所有新增/保留的流程行内注释均采用统一编号格式，且同一函数编号连续。
- [x] 扫描 `src/data_agent/**/*.py` 的全部注释和 Docstring，将项目自有英文 prose 翻译为中文。
- [x] 复核 `Args:`、`Returns:`、`Yields:`、`Raises:` 等章节名和固定技术术语未被误译。

## 2. 行为不变验证

- [x] `rtk python ./.trellis/tasks/07-26-add-core-flow-comments/research/verify_comment_only_changes.py`
- [x] `rtk python ./.trellis/tasks/07-26-add-core-flow-comments/research/verify_step_comments.py`
- [x] `rtk git diff --check`
- [x] 人工确认产品 diff 只包含注释或 Docstring。

## 3. 质量检查

- [x] `rtk uv run ruff check src tests`
- [x] `rtk uv run pyright src tests`
- [x] `rtk uv run python -m compileall -q src tests`
- [x] `rtk uv run pytest -m "not integration"`

## 4. 复核门禁

- [x] 检查代理逐项映射更新后的 PRD R1-R8 与 AC1-AC6。
- [x] 复核无前端、测试、配置、SQL、文档或无关模块产品改动。
- [x] 检查发现的注释准确性问题已修正，并已重跑全部门禁。

## Spec 同步评估

本任务未新增或改变 API、数据库、基础设施或错误处理契约。用户补充确立
“业务 CRUD 和复杂函数内部均需要编号步骤中文注释，且不保留独立未编号
说明性注释”的项目约定，已同步到
`.trellis/spec/backend/quality-guidelines.md`。

## 回滚点

- 在质量检查前保留按文件可识别的注释 diff；发现范围漂移时只移除本任务新增注释，不修改用户或其他任务内容。
