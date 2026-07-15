# TEI CPU 服务与 LangChain Hugging Face 客户端设计

## Architecture

保留单一任务和一条本地调用链：

1. `docs/docker/docker-compose.yml` 启动 Hugging Face TEI CPU 服务。
2. TEI 从 Hugging Face Hub 下载并加载 `BAAI/bge-small-zh-v1.5`，缓存写入命名卷。
3. `conf/app.yaml` 提供 TEI HTTP base URL，`app/conf/app_config.py` 负责校验。
4. `app/clients/tei_embedding_client_manager.py` 通过 `langchain_huggingface` 提供 embedding 客户端与现有风格的单例 manager。

不拆分父子任务：Compose 服务与客户端虽然可分别检查，但客户端集成验收依赖 TEI 服务，改动规模也很小。

## Compose Contract

- Service：`text-embeddings-inference`
- Image：`ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`
- Model：`BAAI/bge-small-zh-v1.5`
- Host endpoint：`http://localhost:8080`
- Container endpoint：port `80`
- Cache：命名卷挂载 `/data`
- Health：Compose healthcheck 调用容器内 `http://localhost:80/health`

不添加 GPU 配置、鉴权、资源限额或模型环境变量；当前需求只有一个固定 CPU 模型。

## Python Contract

新增 `tei` 配置段，只包含 `url`。客户端由一个最小子类和 manager 组成：

- `TeiEmbeddings(HuggingFaceEndpointEmbeddings)`：只覆盖 `aembed_query`，添加 BGE query 指令。
- `TeiEmbeddingClientManager`：沿用 Qdrant/Elasticsearch manager 的 initialize/get/close 生命周期。

Hugging Face client 的 endpoint 固定为 `{tei.url}/embed`，请求参数固定：

- `normalize=True`
- `truncate=True`
- `aembed_documents(texts)`：直接复用 `HuggingFaceEndpointEmbeddings`，不添加指令。
- `aembed_query(text)`：添加 `为这个句子生成表示以用于检索相关文章：`。
- `model_kwargs={"normalize": True, "truncate": True}` 统一传给 TEI。

`HuggingFaceEndpointEmbeddings` 1.2.2 的标准构造器拒绝自托管 HTTP URL，并会同时创建同步和异步客户端。manager 因此使用 Pydantic `model_construct` 注入仅有的 `AsyncInferenceClient`；关闭时只关闭该 async client，再清空 manager 单例。

## Dependencies

新增 `langchain-huggingface>=1.2,<2` 和直接使用的 `huggingface-hub>=1.23,<2`。不引入 `langchain-openai`；不自行实现 documents embedding 或同步占位方法。

## Compatibility and Failure Behavior

- Compose 仅面向当前 x86_64 CPU 开发机；ARM64 镜像不在本任务范围。
- 第一次启动需要访问 Hugging Face Hub 下载模型，之后复用命名卷缓存。
- TEI 未启动、尚未加载完成或返回错误时，保留 `huggingface_hub` 原始异常，不转换成新的项目异常类型。
- query 指令与 512 维断言绑定当前固定模型；未来换模型时必须同步调整客户端与向量集合维度。

## Rollback

移除 Compose service/volume、`tei` 配置、客户端文件和两项 Hugging Face 依赖即可回滚；现有 Qdrant 与 Elasticsearch 数据卷不受影响。
