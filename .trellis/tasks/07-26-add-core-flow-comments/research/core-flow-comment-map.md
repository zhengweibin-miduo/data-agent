# 核心流程注释定位

## DDL 任务受理与事件

- `src/data_agent/ddl_metadata/api/jobs.py:24-74`
  - `submit_job` 在 `DDLJobStore.submit()` 成功后返回 `202`、状态 URL 和 SSE URL。
  - 需要说明：受理承诺是 Redis state、source lease、dispatch outbox 已原子持久化，实际 graph 执行由 worker 异步完成。
  - SSE 读取公开 job 投影和通知流，不直接暴露 LangGraph task payload。
- `src/data_agent/ddl_metadata/jobs/store.py:77-107`
  - `submit()` 完成 UTF-8 大小边界、原子 submit 和尽力发布事件。
  - 避免重复已有实现细节，只强调权威状态先于通知事件。

## Worker 与 LangGraph

- `src/data_agent/ddl_metadata/worker/job_runner.py:232-472`
  - 前置守卫：公开状态、revision、graph version 和 source lease。
  - `thread_id=job_id` 复用 checkpoint；无 snapshot 时注入初始状态，有 snapshot 时继续恢复。
  - interrupt 通过已提交答案构造 `Command(resume=...)`，graph `tasks` stream 只投影稳定业务阶段。
  - 瞬态异常先回到可重试状态并交给 arq；非重试或耗尽后写安全终态并安排 checkpoint 清理。
- `src/data_agent/ddl_metadata/workflow/graph.py:21-51`
  - 流程为解析、记忆加载与重校验、模型分类、确定性验证、指标问题规划/等待回答、指标生成与验证、候选记忆构建、快照持久化。
  - 需要说明：模型输出被确定性校验约束；等待输入时不写权威数据；`persist_snapshot` 是唯一成功写入出口。

## Conversation

- `src/data_agent/conversation/service.py:96-195`
  - `start_turn()` 的事务先持久化用户消息，再在事务外构建可返回上下文。
  - `_context()` 合并当前摘要、摘要游标后的近期消息、同 `user_id` 的长期记忆，并受消息数量与字符预算限制。
  - 注释必须保留同租户边界，避免让读者误解为跨用户召回。
- `src/data_agent/conversation/extraction.py::_validated_candidates`
  - 按候选规范化、证据消息归属/角色、精确 quote、助手结论后续确认、同批逻辑作用域去重和 `MemoryCandidate` 构建分段。
- `src/data_agent/conversation/repository.py`
  - `start_turn`、`complete_turn` 说明会话锁、幂等回放、活动轮次门禁、消息/outbox 原子写入和门禁释放。
  - `claim_extractions`、`finish_extraction`、`retry_extraction` 说明最早任务领取、lease、摘要游标单调推进和失败退避。

## 应用生命周期

- `src/data_agent/application.py:30-67`
  - 生命周期初始化 Redis、MySQL、ES、Qdrant、TEI 等资源后，才把各业务服务装配进 `app.state`。
  - 关闭按依赖的逆序执行，确保部分失败时仍释放已初始化资源。

## Meta 快照持久化

- `src/data_agent/ddl_metadata/persistence/snapshots.py:43-66`
  - 同一 managed Session 中先计算有效 scope/schema 指纹集合并过期不兼容记忆，再同步 Meta，最后写权威记忆及双索引 outbox。
- `src/data_agent/ddl_metadata/persistence/metadata_repository.py:45-173`
  - 同步只清理本次提交表范围内的旧列/关联；先移除受影响列的旧关联再按当前指标重建，最后删除无任何列关联的孤儿指标。

## DDL 节点、事件与解析

- `src/data_agent/ddl_metadata/workflow/nodes.py`
  - 对分类、确定性校验、问题规划/等待回答、指标生成/校验、记忆构建和持久化入口按节点阶段分段。
- `src/data_agent/ddl_metadata/api/job_events.py::stream_job_events`
  - 按权威快照、事件游标读取、超时投影修复/心跳和安全 `stream_error` 降级分段。
- `src/data_agent/ddl_metadata/parsing.py`
  - `_parse_table` 按表身份/约束准备、列解析、约束引用校验分段。
  - `_parse_ddl_sync` 按字节/SQL 解析、语句筛选、表级限制、规范化与指纹构建分段。

## 长期记忆生命周期

- `src/data_agent/memory/mysql/repository.py::upsert_candidates`
  - 按候选策略/活动槽校验、生命周期决策、版本 UID/关联重写、权威行/事件/link 写入、旧版本状态和双目标 outbox 分段。
- 同文件的到期、指纹过期、用户 tombstone 和 purge 函数
  - 说明锁定选择、状态/事件更新、DELETE desired state 和仅在 outbox 清空后物理删除。
- `src/data_agent/memory/application/search.py::search`
  - 说明 MySQL exact 基线、ES/Qdrant 并发降级、候选 UID 权威回查、pending 信号剔除、RRF 和连续权威过滤。

## 明确排除

- 仓库没有前端应用。
- 纯数据契约、表定义、一行式键/标识生成器和工具指令不强制添加步骤注释。
- 业务 CRUD、资源生命周期、配置/日志装配与核心流程均使用连续中文编号步骤。
- 未编号的说明性行内注释删除或并入相邻编号步骤；多行步骤的续行不重复编号。
- 字段映射、测试准备代码和显而易见表达式不逐行复述。
