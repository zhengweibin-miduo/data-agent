# 为核心业务流程补充中文注释：技术设计

## 设计目标

本任务只改善源代码中的流程可读性。产品代码的控制流、数据结构、异常语义、事务边界、公开协议和配置值必须保持不变。

## 边界与落点

| 流程 | 注释落点 | 需要说明的非显然语义 |
| --- | --- | --- |
| DDL 任务受理 | `api/jobs.py`、`jobs/store.py` | `202` 只在状态、租约和 dispatch outbox 持久化后返回；SSE 读取公开投影，不暴露 graph 内部事件 |
| Worker/LangGraph | `worker/job_runner.py` | revision 与租约防止陈旧执行覆盖新状态；checkpoint 决定首次输入或恢复；interrupt 用已提交答案恢复；仅瞬态故障重试 |
| LangGraph 拓扑 | `workflow/graph.py` | LLM 阶段前后由确定性解析/校验约束；等待回答时不持久化；持久化是验证后的唯一出口 |
| Conversation | `conversation/service.py` | 用户消息先在事务中落库；上下文由摘要、近期消息、同租户记忆组成，并按预算裁剪 |
| Conversation 提炼 | `conversation/extraction.py` | 复杂候选校验函数内部按规范化、证据归属、quote、后续确认、去重和构建记忆分段 |
| 应用生命周期 | `application.py` | 服务只能在依赖资源初始化后装配；关闭顺序反向释放依赖 |
| Meta 快照 | `persistence/snapshots.py`、`metadata_repository.py` | 同一事务先约束有效指纹/清理范围，再同步 Meta、权威记忆和 outbox；清理不得越过提交表范围 |

## 注释形式

- 流程总览优先使用函数 Docstring 或阶段代码块上方的短段落。
- 行内说明统一使用“步骤一、步骤二……”编号，放在阶段切换、CRUD 读取/校验/写入/回读、事务/并发边界和容易误改的不变量前。
- 生产代码中不保留独立的未编号说明性注释；将有效语义并入相邻步骤，删除重复内容。工具指令注释不受此约束。
- 长函数包含多个连续筛选或转换阶段时，使用可顺序阅读的中文步骤注释；不使用表示未完成工作的 `TODO`。
- 一个语义只解释一次；调用方说明外部承诺，实现方说明内部不变量。
- 保留 `Args:`、`Returns:`、`Yields:`、`Raises:` 等 Google Style 英文章节名。

## 兼容性

- 不改变 Python AST 中除 Docstring 节点外的任何结构。
- 不改变导入、符号、类型标注、字符串配置值、日志字段、异常、路由或测试。
- 不触碰已有充分注释的 `memory_context.py`、`memory/application/search.py`、`memory/indexing/dispatcher.py` 等区域。

## 风险与控制

- 风险：注释与实现不一致。控制：逐处依据同函数控制流和项目规范复核。
- 风险：CRUD 注释过密。控制：按业务阶段合并说明，不逐行复述字段或 SQL 表达式。
- 风险：误改可执行代码。控制：对所有产品 Python diff 做剥离 Docstring 后的 AST 对比，并执行静态检查与测试。

## 回滚

产品改动仅为注释和 Docstring，可按文件移除本任务新增说明；不需要数据迁移、配置回滚或外部资源操作。
