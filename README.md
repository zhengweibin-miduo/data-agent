# Data Agent

Data Agent 是一个面向 MySQL DDL 的异步元数据生成项目。它使用确定性 DDL 解析、结构化 LLM 输出、LangGraph 可恢复工作流、人工澄清以及长期记忆，生成并持久化语义元数据和指标。

## 环境要求

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 20.19+ 与 npm（独立前端开发和构建）
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

API 与 worker 启动前还需在各自终端设置模型密钥：

```powershell
$env:DATA_AGENT_LLM_API_KEY = "your-api-key"
```

密钥只从环境变量读取，不应写入 YAML 或提交到仓库。

## 运行 API 与 worker

先启动依赖服务，再在两个终端分别运行：

```powershell
uv run data-agent-api
```

API 默认只提供 `/api/v1/**`、OpenAPI 与健康检查，不再托管前端页面。迁移期如需
临时恢复旧入口，可显式设置 `ENABLE_LEGACY_FRONTEND=true`；生产环境应保持默认
关闭。

```powershell
uv run arq data_agent.ddl_metadata.worker.settings.WorkerSettings
```

API 使用 `conf/app_config.yaml` 中的回环地址与端口。worker 保留 arq 官方
discovery 路径，并在启动时检查 Redis checkpoint、MySQL、派生索引、TEI 与
结构化模型能力。

## 运行独立前端

本地开发时，在第三个终端运行：

```powershell
cd frontend
npm ci
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
npm run dev
```

浏览器访问 `http://127.0.0.1:5173/workbench`。后端
`conf/app_config.yaml` 默认允许 `127.0.0.1:5173` 与 `localhost:5173`；部署到其它
Origin 时必须同步收紧 `api.cors_origins`。

生产构建使用：

```powershell
cd frontend
npm ci
$env:VITE_API_BASE_URL = "/api"
npm run build
```

将 `frontend/dist/` 交给静态服务器，并为 `/workbench`、`/knowledge`、
`/workbench/{job_id}` 等前端路由配置 SPA fallback（未知静态路径回退到
`/index.html`）。仓库提供可直接调整的 `frontend/deploy/nginx.conf` 和
`frontend/deploy/Caddyfile` 示例。把同域 `/api/` 反向代理到
`http://127.0.0.1:8000/api/`。SSE 代理必须关闭缓冲和缓存，读取超时应长于后端
心跳间隔，例如 Nginx location 中使用 `proxy_buffering off`、
`proxy_cache off`、`proxy_read_timeout 3600s`。分域部署时，将
`VITE_API_BASE_URL` 设置为完整 API Origin，并在后端将 `api.cors_origins` 精确配置为
实际前端 Origin，同时显式设置 `api.allow_remote_cors_origins: true`。该开关只放宽
CORS 配置校验，不会改变 API 的回环监听地址；对外发布仍应由带认证和访问控制的代理承担。
示例代理默认只监听 `127.0.0.1:80`，与无认证的本地单用户边界保持一致；如需
向局域网或公网开放，必须先增加认证，并通过防火墙或其它网络访问控制限制来源。

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
cd frontend
npm ci
npm run lint
npm run typecheck
npm run test
npm run build
cd ..
git diff --check
```

MySQL 与 Redis 可用时可继续运行集成测试（不启动可选 TEI 测试）：

```powershell
uv run pytest -m "integration and not tei"
```

完整本地依赖均可用时，使用 `uv run pytest -m "not tei"` 运行与 CI 相同的
MySQL/Redis 测试集合。TEI live 检查需显式运行对应的
`tests/integration/infrastructure/test_tei_embeddings.py`。
