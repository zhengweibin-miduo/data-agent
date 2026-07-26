# 源码与测试注释盘点

## 盘点结果

- `src/`：85 个 Python 文件，约 520 处 AST Docstring、2 处 tokenize 行注释、未发现 `TODO/FIXME/NOTE/HACK/XXX`。
- `tests/`：47 个 Python 文件，约 252 处 AST Docstring、未发现普通行注释或上述待办标记。
- 项目以中文短 Docstring 为主，保留 `DDL`、`outbox`、`Store`、`Mem0` 等英文技术词。

## 高密度位置

- `src/data_agent/ddl_metadata/jobs/store.py`：32 处 Docstring。
- `src/data_agent/ddl_metadata/models/memory.py`：31 处 Docstring。
- `src/data_agent/ddl_metadata/memory/mysql/repository.py`：27 处 Docstring。
- `src/data_agent/settings.py`：19 处 Docstring，另有 1 处普通注释。
- `src/data_agent/conversation/models.py`：18 处 Docstring。
- `src/data_agent/conversation/repository.py`：17 处 Docstring。
- `src/data_agent/ddl_metadata/workflow/nodes.py`：15 处 Docstring。
- `src/data_agent/conversation/api.py`：15 处 Docstring。
- `tests/unit/ddl_metadata/memory/test_search.py`：27 处 Docstring。
- `tests/unit/ddl_metadata/test_job_events.py`：20 处 Docstring。

## 代表性证据

- `src/data_agent/settings.py:341`：`# 仅加载一次，供所有应用模块共享。`
- `src/data_agent/ddl_metadata/memory/application/search.py:203`：`except Exception as exc:  # noqa: BLE001`
- `src/data_agent/ddl_metadata/jobs/store.py:1`：`DDL 任务生命周期应用门面。`
- `src/data_agent/ddl_metadata/jobs/store.py:41`：`组合专职 Store 并编排 DDL 任务业务生命周期。`
- `src/data_agent/ddl_metadata/jobs/store.py:78`：`原子写入任务和 outbox 后才报告受理。`
- `src/data_agent/ddl_metadata/models/memory.py:1`：`Mem0 风格长期语义记忆的领域契约。`
- `src/data_agent/ddl_metadata/models/memory.py:22`：`内置记忆类别常量；持久化字段允许扩展的点分字符串。`
- `src/data_agent/ddl_metadata/workflow/nodes.py:69`：`解析 DDL 并初始化工作流状态。`
- `tests/unit/ddl_metadata/memory/test_search.py:1`：`记忆搜索服务的单元契约测试。`

## 建议审查规则

1. 优先核对 Docstring 与真实签名、异常、副作用、事务、并发和生命周期是否一致。
2. 仅保留对外契约或非显然业务规则，识别逐方法机械复述实现的占位 Docstring。
3. 统一中文领域术语，同时保留准确的英文协议名、配置键和代码标识符。
4. 测试 Docstring 应描述被验证的契约，而不是重复测试实现步骤。
5. `noqa` 属于工具抑制说明，应核验规则代码和异常处理理由；不能仅按普通注释风格判断。
6. 若未来出现 TODO，要求带 owner/issue 和明确完成或移除条件。
