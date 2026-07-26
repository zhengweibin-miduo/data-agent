# 项目结构修复设计

## 1. 目标结构

```text
src/data_agent/
├── application.py
├── errors.py
├── identifiers.py
├── models/
│   ├── base.py
│   ├── jobs.py
│   ├── memory.py
│   ├── physical.py
│   └── semantic.py
├── persistence/
│   └── schema.py
├── memory/
│   ├── application/
│   ├── domain/
│   ├── indexing/
│   └── mysql/
├── conversation/
├── ddl_metadata/
│   ├── api/
│   ├── jobs/
│   ├── persistence/
│   ├── workflow/
│   └── worker/
└── infrastructure/
```

## 2. 所有权与依赖方向

- `data_agent.models` 拥有跨 HTTP、workflow、persistence、conversation 和 memory 使用的类型化契约。
- `data_agent.memory` 拥有长期记忆的领域规则、application use cases、MySQL adapter 和派生索引 adapter。
- `data_agent.persistence.schema` 只拥有全应用共享的 SQLAlchemy `MetaData`。
- `data_agent.errors` 拥有应用可映射到 HTTP 的稳定业务错误；内部类名提升为 `DataAgentError`，JSON `code/stage/retryable/details` 契约不变。
- `data_agent.identifiers` 拥有跨 conversation、memory 和 DDL workflow 使用的稳定标识符。
- `data_agent.ddl_metadata` 只保留 DDL API、任务、快照持久化、DDL 记忆引用校验、workflow 和 worker。
- `data_agent.conversation` 依赖根级 models/memory/persistence/errors/identifiers，不再穿透 DDL 包。

目标依赖：

```text
application
  -> conversation + ddl_metadata.api + memory.application
  -> infrastructure lifecycle

conversation
  -> models + memory + persistence.schema + infrastructure.mysql

ddl_metadata
  -> models + memory + errors + identifiers
  -> jobs + persistence + workflow + worker

memory
  -> models + identifiers + persistence.schema
  -> infrastructure adapters
```

## 3. 模块深度

本次移动真实实现，不新增转发 facade。`MemoryService`、`MemorySearchService`、`DDLJobStore` 等现有 application-facing interface 保持行为；调用方更新 owning module path。DDL 专用 `MemoryContextLoader` 归入 DDL workflow。根级 `MemoryService` 只声明来源租约与外部引用校验两个小 interface，由 `DDLJobStore` 和 DDL persistence adapter 在组合根注入，因此根级 memory 不反向导入 DDL。MySQL、ES、Qdrant、TEI 仍使用现有 adapter，不为单一生产实现制造新的 port。

## 4. Domain 版本输入

在 `memory.domain.candidates` 定义不可变 `MemoryVersions`：

```python
@dataclass(frozen=True, slots=True)
class MemoryVersions:
    content: str
    projection: str
```

`build_accepted_memories(..., *, versions: MemoryVersions, job_id=...)` 与内部 `_candidate` 显式接收该值。workflow node 和 `MetadataSnapshotService` 在 application seam 从 `app_config.memory` 构造值并传入。版本只填充候选字段，不参与 UID 或 content hash。

## 5. 兼容性约束

- 不改变 HTTP 路由、方法、状态码、响应字段和错误 JSON。
- 不改变 Pydantic 类名、字段、枚举值和校验行为。
- 不改变 MySQL schema/table/column/index、Redis key/hash/Lua、arq 函数/cron、LangGraph state/node/edge。
- 不改变 YAML 键 `memory.content_version` 与 `memory.projection_version`。
- Python 内部旧路径执行硬迁移；当前规范明确不保留内部兼容 shim。

## 6. 本地运行与 CI

- Compose 的 MySQL、Qdrant、Elasticsearch、TEI 与 Redis 均绑定 `127.0.0.1`。
- `pyproject.toml` 增加 `data-agent-api = "data_agent.main:main"`。
- worker 继续记录为 `uv run arq data_agent.ddl_metadata.worker.settings.WorkerSettings`，不封装不稳定的 arq CLI 内部接口。
- README 记录最小启动和验证顺序。
- CI 在质量检查中运行 `docker compose -f docs/docker/docker-compose.yml config`，只渲染配置，不启动可选全栈。

## 7. 迁移与回滚

迁移在单一工作分支原子完成：先移动 owning modules，再全量更新活动导入和规范，最后删除旧路径。若验证失败，按提交前完整 diff 回退本任务改动；不修改数据库数据或远端状态，因此无需数据回滚。
