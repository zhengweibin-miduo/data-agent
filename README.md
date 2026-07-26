# Data Agent

Data Agent 是一个面向 MySQL DDL 的异步元数据生成项目。它使用确定性 DDL 解析、结构化 LLM 输出、LangGraph 可恢复工作流、人工澄清以及长期记忆，生成并持久化语义元数据和指标。

## 环境要求

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Docker 与 Docker Compose
- 一个支持结构化输出的 OpenAI 兼容模型端点

安装锁定依赖：

```powershell
uv sync --locked
```

## 启动依赖服务

本地 Compose 提供 MySQL、Redis、Qdrant、Elasticsearch 和 TEI，所有宿主机
端口都只绑定 `127.0.0.1`：

```powershell
docker compose -f docs/docker/docker-compose.yml up -d
docker compose -f docs/docker/docker-compose.yml ps
```

MySQL 官方镜像只会在全新的 `mysql_data` 卷上运行
`docs/docker/mysql/` 中的初始化脚本，自动创建 `data_agent`、`dw` 和 `meta`
数据库。已有卷不会重新执行脚本；这些 SQL 是空白环境 bootstrap，不是升级
迁移，手工重放可能覆盖本地表，不要对包含有用数据的共享卷执行。

应用连接、索引名称和服务地址位于 `conf/app_config.yaml`。配置文件位置按以下顺序
解析，命中即停止：

1. `DATA_AGENT_CONFIG` 环境变量指定的文件；
2. 当前工作目录下的 `conf/app_config.yaml`；
3. 源码树相对位置（仓库根的 `conf/app_config.yaml`）。

`conf/` 不随 wheel 一起打包，因此以已安装包运行时必须用 `DATA_AGENT_CONFIG`
指定配置，或从包含 `conf/` 的部署目录启动：

```powershell
$env:DATA_AGENT_CONFIG = "D:\deploy\data-agent\conf\app_config.yaml"
```

`DATA_AGENT_CONFIG` 指向的文件不存在时启动会直接失败，不会回退到其它候选位置——
显式指定被静默忽略比启动失败更难排查。全部候选都缺失时，报错会列出实际查找过的
绝对路径。

worker 启动前还需在当前终端设置模型密钥：

```powershell
$env:DATA_AGENT_LLM_API_KEY = "your-api-key"
```

密钥只从环境变量读取，不应写入 YAML 或提交到仓库。

## 运行 API 与 worker

先启动依赖服务，再在两个终端分别运行：

```powershell
uv run data-agent-api
```

```powershell
uv run arq data_agent.ddl_metadata.worker.settings.WorkerSettings
```

API 使用 `conf/app_config.yaml` 中的回环地址与端口。worker 保留 arq 官方
discovery 路径，并在启动时检查 Redis checkpoint、MySQL、派生索引、TEI 与
结构化模型能力。

## 验证

不依赖本地服务的基础质量门禁：

```powershell
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv run pytest -m "not integration"
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

MySQL 与 Redis 可用时可继续运行集成测试（不启动可选 TEI 测试）：

```powershell
uv run pytest -m "integration and not tei"
```

完整本地依赖均可用时，使用 `uv run pytest -m "not tei"` 运行与 CI 相同的
MySQL/Redis 测试集合。TEI live 检查需显式运行对应的
`tests/integration/infrastructure/test_tei_embeddings.py`。
