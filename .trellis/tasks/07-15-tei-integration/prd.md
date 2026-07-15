# 接入 TEI CPU 服务与客户端

## Goal

在本地 Docker Compose 环境中运行 CPU 版 Text Embeddings Inference（TEI），并在 Python 应用中通过 `langchain_huggingface` 提供异步 embedding 客户端，供后续向量化流程直接复用。

## Background

- 当前 `docs/docker/docker-compose.yml` 仅包含 Qdrant 与 Elasticsearch。
- 当前应用配置仅包含日志、Qdrant 和 Elasticsearch，客户端采用 `app/clients/*_client_manager.py` 的单例生命周期管理模式。
- 项目尚无 embedding 客户端。
- TEI 1.9 提供 x86_64 CPU 镜像及 Hugging Face 原生 `/embed` 接口。

## Requirements

- 在现有 Compose 文件中新增一个 CPU-only TEI 服务，不要求 GPU、CUDA 或 NVIDIA runtime。
- 将模型缓存持久化，容器重建后不重复下载已缓存权重。
- 暴露本机 HTTP 端口，供仓库中的 Python 应用直接访问。
- 将 TEI 连接地址纳入现有 YAML + Pydantic 配置体系，并保持未知字段校验。
- 使用 `langchain_huggingface.HuggingFaceEndpointEmbeddings` 提供 query 与 documents embedding，不创建同步 Hugging Face 客户端，也不自行实现同步占位方法。
- 客户端复用现有 manager 风格，不另建通用工厂或额外抽象层。
- 使用 Hugging Face 原生链路：模型从 Hugging Face Hub 加载，LangChain 客户端内部通过 `huggingface_hub` 调用 TEI `/embed`。
- 不使用 TEI 的 OpenAI-compatible `/v1/embeddings` 接口。
- 固定使用 `BAAI/bge-small-zh-v1.5`，输出 512 维向量。
- documents 不添加检索指令；query 按模型建议增加中文检索指令，所有向量归一化。

## Acceptance Criteria

- [ ] `docker compose config` 能成功解析修改后的 Compose 文件。
- [ ] TEI 容器仅使用 CPU 镜像启动，并通过宿主机端口提供健康检查和 embedding HTTP API。
- [ ] TEI 模型文件写入命名卷，重建容器后可复用缓存。
- [ ] 应用配置能校验并读取 TEI base URL。
- [ ] 客户端返回 `HuggingFaceEndpointEmbeddings` 实例，重复初始化时复用同一实例，且只初始化异步 Hugging Face 客户端。
- [ ] 异步 query 和 documents embedding 能通过 Hugging Face 客户端面向 TEI `/embed` 工作。
- [ ] 在 `app_test/clients` 留下一个最小可运行测试，验证异步 query 与 documents 的结果数量正确且每个向量均为 512 维。

## Out of Scope

- GPU/CUDA 镜像与运行参数。
- embedding 结果写入 Qdrant 的业务流程。
- reranker、稀疏向量、鉴权、模型热切换与生产级扩缩容。

## Notes

- Compose 服务与客户端共同组成同一条本地 embedding 链路，保留为一个任务，不拆父子任务。
