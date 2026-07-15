# TEI CPU 服务与 LangChain Hugging Face 客户端实施计划

## Ordered Checklist

1. 使用 `uv add "langchain-huggingface>=1.2,<2" "huggingface-hub>=1.23,<2"` 更新 `pyproject.toml` 与 `uv.lock`。
2. 修改 `docs/docker/docker-compose.yml`：新增 TEI CPU service、8080 端口、模型缓存卷和 healthcheck。
3. 修改 `conf/app.yaml` 与 `app/conf/app_config.py`：新增并校验 `tei.url`。
4. 新增 `app/clients/tei_embedding_client_manager.py`：管理 `HuggingFaceEndpointEmbeddings` 的本地 TEI 异步客户端生命周期。
5. 在 `app_test/clients/test_tei_embedding_client_manager.py` 中保留最小 live integration test，覆盖 query/documents 的异步调用及 512 维结果。

## Validation

依次执行：

```powershell
docker compose -f docs/docker/docker-compose.yml config
docker compose -f docs/docker/docker-compose.yml up -d text-embeddings-inference
docker compose -f docs/docker/docker-compose.yml ps text-embeddings-inference
uv run python -m app.conf.app_config
uv run python -m app_test.clients.test_tei_embedding_client_manager
uv run python -m compileall app
```

首次 TEI 启动会下载模型，必须等 healthcheck 通过后再运行 live check。验证完成后不删除命名卷，保留模型缓存。

## Review Gates

- Compose 展开配置中不得出现 GPU runtime/device 配置。
- 镜像必须是 `cpu-1.9`，模型必须是 `BAAI/bge-small-zh-v1.5`。
- query 才能添加检索指令，documents 不得添加。
- manager 只创建 `AsyncInferenceClient`，不得初始化同步 `InferenceClient`。
- embedding 实例必须来自 `langchain_huggingface`，异步路径不得改走 OpenAI API。
- 不新增通用工厂、重试框架、自定义异常层或 Qdrant 写入逻辑。

## Risk and Rollback Points

- Docker/GHCR 或 Hugging Face Hub 网络失败：保留代码，报告外部下载阻塞；不要替换成其他 registry/model。
- 模型输出非 512 维：停止集成，不修改 Qdrant collection 猜测性兼容。
- Python 3.14 依赖解析失败：停止并报告具体包冲突，不放宽项目 Python 版本。
- 回滚按 `design.md` 的文件边界进行，不触碰现有服务的数据卷。
