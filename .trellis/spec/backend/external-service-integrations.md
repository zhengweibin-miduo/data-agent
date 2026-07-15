# External Service Integrations

## Scenario: Local Text Embeddings Inference

### 1. Scope / Trigger

Use this contract when changing the local TEI Compose service, its application configuration, or its LangChain Hugging Face client. It prevents the server model, vector shape, and query encoding behavior from drifting independently.

### 2. Signatures

```python
class TeiEmbeddings(HuggingFaceEndpointEmbeddings):
    async def aembed_query(self, text: str) -> list[float]: ...

class TeiEmbeddingClientManager:
    @classmethod
    def initialize(cls) -> TeiEmbeddings: ...
    @classmethod
    def get_client(cls) -> TeiEmbeddings: ...
    @classmethod
    async def close(cls) -> None: ...
```

### 3. Contracts

- Compose service: `text-embeddings-inference`.
- Image: `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`; no GPU device requests.
- Model: `BAAI/bge-small-zh-v1.5`; output dimension is 512.
- Endpoint: `conf/app.yaml` key `tei.url`, with Hugging Face requests sent to `{url}/embed`.
- Cache: named volume mounted at `/data`.
- Documents receive no query instruction; the LangChain client replaces newlines with spaces. Queries prepend `为这个句子生成表示以用于检索相关文章：`.
- All requests use `normalize=True` and `truncate=True`.
- The managed embedding instance comes from `langchain_huggingface`; only the BGE query instruction is overridden locally.
- Construct it with Pydantic `model_construct`, inject `AsyncInferenceClient(model="{url}/embed")`, and set `client=None` because the standard constructor rejects self-hosted URLs and creates a sync client for accepted repo IDs.
- Pass `normalize=True` and `truncate=True` through `model_kwargs`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| `tei.url` missing or unknown config key present | Pydantic configuration validation fails at startup |
| `get_client()` called before `initialize()` | Raise `RuntimeError` with the initialization instruction |
| Standard `HuggingFaceEndpointEmbeddings(model="http://...")` construction | Rejects the URL; do not use this path |
| Managed client initialization | `client is None` and `async_client` targets `{url}/embed` |
| Input exceeds model token limit | TEI truncates it because `truncate=True` |
| TEI unavailable or returns an HTTP error | Preserve the original `huggingface_hub` exception |
| Model output is not 512 dimensions | Integration test fails; do not guess a Qdrant dimension |

### 5. Good / Base / Bad Cases

- Good: initialize once, reuse the manager client, call `aembed_documents` / `aembed_query`, then close it during application shutdown.
- Base: a one-item documents batch returns one normalized 512-dimensional vector.
- Bad: use the standard endpoint constructor for a self-hosted URL, create a sync inference client, omit normalization, or change the Compose model without updating the query instruction and vector dimension.

### 6. Tests Required

```powershell
docker compose -f docs/docker/docker-compose.yml config
docker compose -f docs/docker/docker-compose.yml up -d text-embeddings-inference
uv run python -m app.conf.app_config
uv run python -m app_test.clients.test_tei_embedding_client_manager
```

The integration test must assert the LangChain client type, `client is None`, async query/document calls, normalized vectors, and 512 dimensions. Compose inspection must show a healthy container, the `/data` volume, and no GPU device request.

### 7. Wrong vs Correct

```python
# Wrong: the standard constructor rejects a self-hosted URL.
HuggingFaceEndpointEmbeddings(model="http://localhost:8080/embed")

# Correct: manager injects only the async client.
HuggingFaceEndpointEmbeddings.model_construct(
    client=None,
    async_client=AsyncInferenceClient(model="http://localhost:8080/embed"),
)
```
