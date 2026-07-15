# TEI 与 LangChain 接入调研

调研日期：2026-07-15

## 仓库现状

- Compose：`docs/docker/docker-compose.yml`，当前包含 Qdrant 1.18.0 与 Elasticsearch 8.19.17。
- 配置：`conf/app.yaml` + `app/conf/app_config.py`，Pydantic `extra="forbid"`。
- 客户端：`app/clients/qdrant_client_manager.py` 与 `app/clients/elasticsearch_client_manager.py` 使用类级单例模式。
- 依赖：`pyproject.toml` 尚无 LangChain 依赖，Python 要求 `>=3.14`。

## TEI

- Hugging Face 最新稳定 release 为 `v1.9.3`；官方镜像矩阵为 x86_64 CPU 提供 `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`。
- 官方启动约定：容器内监听 80，模型缓存挂载到 `/data`，通过 `--model-id` 指定 Hugging Face 模型。
- TEI 同时提供原生 `/embed` 和 OpenAI-compatible `/v1/embeddings`。
- 官方 quick tour 的 OpenAI 示例使用 `base_url="http://localhost:8080/v1"` 与占位 API key。

来源：

- https://github.com/huggingface/text-embeddings-inference/releases/tag/v1.9.3
- https://github.com/huggingface/text-embeddings-inference#docker-images
- https://huggingface.co/docs/text-embeddings-inference/quick_tour

## LangChain Hugging Face 客户端选择

用户要求客户端管理使用 `langchain_huggingface`，同时不创建同步 `InferenceClient`。最终使用 `HuggingFaceEndpointEmbeddings` 承担 embedding 行为，并向其注入只连接 TEI `/embed` 的 `AsyncInferenceClient`。

理由：

- TEI 官方 quick tour 推荐使用 `huggingface_hub.InferenceClient` 调用自托管服务。
- `HuggingFaceEndpointEmbeddings` 负责 documents embedding、响应转换和 LangChain 接口，业务代码不自行维护裸 HTTP 请求。
- 当前 `langchain-huggingface==1.2.2` 的标准构造器会拒绝以 `http://` 或 `https://` 开头的自托管 endpoint，并无条件创建同步和异步 Hugging Face 客户端。
- 使用公开的 Pydantic `model_construct` 绕过上述构造器校验，直接注入 `AsyncInferenceClient`；`client=None` 保证 manager 不创建同步客户端。
- BGE query 指令仍需区分 documents，因此仅用一个三行子类覆盖 `aembed_query`，其余 embedding 行为全部复用 `HuggingFaceEndpointEmbeddings`。

实现时让 TEI 负责模型 tokenization、batching 与 truncation；LangChain 客户端负责请求和响应适配。

版本快照：

- `langchain-huggingface==1.2.2` 提供 `HuggingFaceEndpointEmbeddings`。
- `huggingface-hub` 提供注入的 `AsyncInferenceClient`。

来源：

- https://pypi.org/project/huggingface-hub/
- https://pypi.org/project/langchain-huggingface/
- https://github.com/langchain-ai/langchain/blob/master/libs/partners/huggingface/langchain_huggingface/embeddings/huggingface_endpoint.py

## 模型决定

固定使用 `BAAI/bge-small-zh-v1.5`：

- Hugging Face 标记为 `text-embeddings-inference` compatible，适合中文 feature extraction。
- 输出维度 512、最大序列长度 512，规模小于 base/large 版本，适合本地 CPU 模式。
- 模型卡建议检索 query 添加 `为这个句子生成表示以用于检索相关文章：`，documents/passages 不添加。
- 模型卡示例使用归一化向量计算相似度，因此客户端显式请求 normalized embeddings。

来源：

- https://huggingface.co/BAAI/bge-small-zh-v1.5
